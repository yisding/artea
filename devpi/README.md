# devpi — PyPI pull-through cache (internal only)

This container is Artea's PyPI mirror layer. It does exactly two things:

1. **`root/pypi`** — devpi's built-in mirror index of `https://pypi.org/simple/`,
   created automatically by `devpi-init` on first boot. Packages are fetched from
   PyPI on demand and cached on disk.
2. **`root/constrained`** — a [`devpi-constrained`](https://pypi.org/project/devpi-constrained/)
   index (`type=constrained`, `bases=root/pypi`) that re-exposes the mirror filtered
   by a constraints list (`name<2`, `name ==1.2.3`, `*` default-deny). The entrypoint
   creates this index idempotently on every boot; a **freshly created** index is
   seeded with the `*` constraint (fail-closed: block everything) and an existing
   index's constraints are **never touched** — the real policy is pushed by
  `policy-sync` from `${ARTEA_NAMESPACE}/registry-policy:pypi-constraints.txt`.

What this container is **not**:

- **No private packages.** Wheels uploaded by users live in Gitea, never here.
- **No auth.** devpi runs wide open; the gateway enforces auth on every devpi-bound
  path via `auth_request` against Gitea. **Never expose port 3141 to the host or
  publish it in compose** — it must only be reachable on the internal docker network
  by the `gateway` container. (`--restrict-modify root` is passed as defense in depth
  so an anonymous reacher cannot create users/indexes, but it is not the auth model.)
- **Not a store of record.** The whole server dir is a disposable cache: it is always
  safe to `docker compose down && docker volume rm <project>_devpi-data` (or
  `make clean`). The next boot re-runs `devpi-init` and recreates both indexes.
  After a wipe, `root/constrained` is seeded **fail-closed** (`*` = block
  everything, e2e S15) until policy-sync's startup sync, webhook, or poll
  replaces the constraints with the real `pypi-constraints.txt`.

## URL shapes (contract for the gateway author)

The gateway's pypi 404-fallback targets the **constrained** index, not the raw mirror:

| What | URL on devpi (`http://devpi:3141`) |
|------|------------------------------------|
| Simple index for a project (PEP 503) | `/root/constrained/+simple/{name}/` (trailing slash; `{name}` PEP 503-normalized — pip normalizes before requesting) |
| Full project list | `/root/constrained/+simple/` (avoid: forces a full pypi.org project-list sync) |
| Release files | `/root/pypi/+f/{hash}/{filename}#sha256=...` — note **`root/pypi`**, not `root/constrained`: cached mirror files live on the base index even when discovered via the constrained one |
| Health/status | `/+status` (also used by the image's `HEALTHCHECK`) |

Because the server runs with `--outside-url http://localhost:8080` **and
`--absolute-urls`**, the file links inside simple pages are absolute URLs under
the gateway origin, verified live: `http://localhost:8080/root/pypi/+f/...`.
(Without `--absolute-urls` devpi emits relative hrefs like `../../../pypi/+f/...`,
which break behind the gateway's `/pypi/simple/` → `/root/constrained/+simple/`
path translation — do not remove that flag.) The gateway must route the
**entire `/root/` prefix** to `devpi:3141` unchanged (after its auth_request guard),
per `docs/ARCHITECTURE.md`. Gitea-stored files use `/api/packages/...` paths and
route to Gitea instead.

Two more gateway notes, both verified against the real server:

- **Always request the simple page with a trailing slash.** For
  `/root/constrained/+simple/{name}` (no slash) devpi answers
  `302 Location: http://localhost:8080/root/constrained/+simple/{name}/` — i.e. a
  devpi-shaped absolute URL on the gateway origin. A client following it skips the
  Gitea-first precedence check for that request, so the gateway's fallback proxy
  should append the slash itself rather than relay the redirect.
- **Hardening (optional):** the only paths clients legitimately reach on devpi are
  `/root/constrained/+simple/...` and the file routes `/root/pypi/+f/...` (and
  `/root/pypi/+e/...`, devpi's external-link route). Restricting the gateway's
  `/root/` location to those keeps anyone from browsing the unfiltered
  `root/pypi/+simple/` mirror through the gateway.

First-boot note: the container reports healthy as soon as `/+status` responds, which
can be a second or two before `root/constrained` exists on the very first boot; a
fallback hitting that window gets a devpi 404 and pip simply reports "not found" —
retrying succeeds.

## Constraints management (contract for the policy-sync author)

Constraints are the `constraints` config key (list of requirement lines) on
`root/constrained`. Only `root` may modify it; devpi accepts HTTP Basic
`root:$DEVPI_ROOT_PASSWORD` directly, so no login dance is needed.

**Use the JSON API, not devpi-client.** Because the server runs with
`--outside-url`, its `/+api` discovery response makes devpi-client rewrite its
target URL to `http://localhost:8080/` — which from inside the docker network is
not devpi (and goes through gateway auth). Raw HTTP against `http://devpi:3141`
is unaffected: `--outside-url` only changes *generated link URLs in responses*,
never request routing. This is also why `entrypoint.sh` ensures the index via
`ensure_index.py` (stdlib urllib) instead of devpi-client.

```text
GET   http://devpi:3141/root/constrained   Accept: application/json
      -> {"result": {..., "constraints": [...]}}    # current config
PATCH http://devpi:3141/root/constrained   Accept: application/json
      Authorization: Basic base64(root:$DEVPI_ROOT_PASSWORD)
      Content-Type: application/json
      body: the full config dict from GET .result with "constraints"
            replaced by the new list of lines, e.g. ["urllib3<2"]
```

The value may be a list of lines or one raw multi-line string — devpi-constrained
normalizes strings itself, skipping blanks and `#` comments (verified in its
source), so policy-sync can push the `pypi-constraints.txt` content as-is.
Setting `"constraints": []` (or an empty string) restores **allow-all** — that is
the plugin's no-constraints behavior; a single `*` line is default-deny.

## Persistence

| Item | Value |
|------|-------|
| Server/cache dir | `/devpi/server` (`DEVPISERVER_SERVERDIR`, set in the image) |
| Compose mount | named volume `devpi-data` → `/devpi/server` |
| Ownership | uid 1000 (`devpi` user); use a named volume, not a host bind-mount, so docker seeds ownership from the image |

## Environment variables

| Var | Default | Purpose |
|-----|---------|---------|
| `DEVPI_ROOT_PASSWORD` | *(required)* | sets the root password at first init; used as HTTP Basic auth by `ensure_index.py` and by policy-sync; from `.env`. Rotating it after init requires changing the root password on the server too, or just wiping the volume |
| `DEVPI_OUTSIDE_URL` | `http://localhost:8080` | public base URL baked into generated links |
| `DEVPI_PORT` | `3141` | listen port (contract value; only override in tests) |
| `DEVPI_STARTUP_TIMEOUT` | `60` | seconds to wait for the server before the entrypoint gives up |
| `DEVPI_ONESHOT` | `0` | `1` = init + ensure index, then exit (used by tests) |

## Version pins

Pinned via build args (defaults in the `Dockerfile`, overridden from `.env` by
compose so all pins live there):

```
DEVPI_SERVER_VERSION=6.20.1
DEVPI_CONSTRAINED_VERSION=2.1.0
```

Compose wiring:

```yaml
devpi:
  build:
    context: ./devpi
    args:
      DEVPI_SERVER_VERSION: ${DEVPI_SERVER_VERSION}
      DEVPI_CONSTRAINED_VERSION: ${DEVPI_CONSTRAINED_VERSION}
  environment:
    DEVPI_ROOT_PASSWORD: ${DEVPI_ROOT_PASSWORD}
  volumes:
    - devpi-data:/devpi/server
  # no `ports:` — internal only, reached via the gateway
```

## Tests

`tests/test_entrypoint.py` runs the real `entrypoint.sh` and `ensure_index.py`
against fake `devpi-init`/`devpi-server` binaries (the fake server really listens
and emulates the index JSON API, so the readiness probe and HTTP calls are
exercised for real). It asserts: init runs only on an empty server dir, the
constrained index is created only when missing (with root credentials and the
fail-closed `*` constraints seed — never overwriting an existing index), re-runs
are no-ops, and a missing `DEVPI_ROOT_PASSWORD` fails fast. No docker or network
needed:

```sh
python3 -m pytest devpi/tests/ -q
```
