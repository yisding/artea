# devpi — PyPI pull-through cache (internal only)

This container is Artea's PyPI mirror layer. It does exactly two things:

1. **`root/pypi`** — devpi's built-in mirror index of `https://pypi.org/simple/`,
   created automatically by `devpi-init` on first boot. Packages are fetched from
   PyPI on demand and cached on disk.
2. **`root/constrained`** — a `type=constrained` index provided by Artea's local
   devpi policy plugin (`artea_devpi_policy`, derived from devpi-constrained's
   small stage customizer). It re-exposes the mirror filtered by a constraints
   list (`name<2`, `name ==1.2.3`, `*` default-deny) and the shared
   `min_upstream_age` ISO 8601 duration. The entrypoint creates this index
   idempotently on every boot; a **freshly created** index is seeded with the `*`
   constraint (fail-closed: block everything) and `min_upstream_age=P0D`. An
   existing index's policy keys are **never touched** — the real policy is pushed
   by `policy-sync` from `${ARTEA_NAMESPACE}/registry-policy`.

What this container is **not**:

- **No private packages.** Wheels uploaded by users live in Gitea, never here.
- **No auth.** devpi runs wide open; the gateway enforces auth on every devpi-bound
  path via `auth_request` against Gitea. **Never expose port 3141 outside the
  cluster** — it must only be reachable in-cluster by the gateway (no Service of
  type other than ClusterIP, no Ingress). (`--restrict-modify root` is passed as defense in depth
  so an anonymous reacher cannot create users/indexes, but it is not the auth model.)
- **Not a store of record.** The whole server dir is a disposable cache (the
  `artea-devpi-data` PVC): it is always safe to delete the PVC and let the chart
  recreate it (`kubectl -n artea delete pvc artea-devpi-data` then
  `kubectl -n artea rollout restart deploy/artea-devpi`). The next boot re-runs
  `devpi-init` and recreates both indexes. After a wipe, `root/constrained` is
  seeded **fail-closed** (`*` = block everything, e2e S15) until policy-sync's
  startup sync, webhook, or poll replaces the constraints and upstream age with
  the real policy.

## URL shapes (contract for the gateway author)

The gateway's pypi 404-fallback targets the **constrained** index, not the raw mirror:

| What | URL on devpi (`http://devpi:3141`) |
|------|------------------------------------|
| Gateway-internal simple index target for a project (PEP 503) | `/root/constrained/+simple/{name}/` (trailing slash; `{name}` PEP 503-normalized — pip normalizes before requesting) |
| Gateway-internal full project list | `/root/constrained/+simple/` (avoid: forces a full pypi.org project-list sync) |
| Release files | `/root/pypi/+f/{hash}/{filename}#sha256=...` — note **`root/pypi`**, not `root/constrained`: cached mirror files live on the base index even when discovered via the constrained one |
| Health/status | `/+status` (also used by the image's `HEALTHCHECK`) |

Because the server runs with `--outside-url http://localhost:8080` **and
`--absolute-urls`**, the file links inside simple pages are absolute URLs under
the gateway origin, verified live: `http://localhost:8080/root/pypi/+f/...`.
(Without `--absolute-urls` devpi emits relative hrefs like `../../../pypi/+f/...`,
which break behind the gateway's `/pypi/simple/` → `/root/constrained/+simple/`
path translation — do not remove that flag.) The gateway proxies
client-facing `/pypi/simple/...` requests to devpi's constrained simple pages
after the Gitea-first private-name check, but raw `/root/*/+simple/...` routes
are not client-visible. The only `/root/` paths exposed on the public origin are
authenticated file/external-link routes: `/root/pypi/+f/...` and
`/root/pypi/+e/...`; before proxying them, the gateway calls the internal
`/+artea/file-allowed?path=...` endpoint exposed by this plugin (which derives
the project from the mirror file at that path, not from the request), so
stale direct file URLs for newly blocked versions return 403 without nginx
buffering and scanning large simple pages. The same plugin also guards direct
`root/pypi` file URLs so they cannot bypass `min_upstream_age`. Gitea-stored files use
`/api/packages/...` paths and route to Gitea instead.

Two more gateway notes, both verified against the real server:

- **Always request the simple page with a trailing slash.** For
  `/root/constrained/+simple/{name}` (no slash) devpi answers
  `302 Location: http://localhost:8080/root/constrained/+simple/{name}/` — i.e. a
  devpi-shaped absolute URL on the gateway origin. A client following it skips the
  Gitea-first precedence check for that request, so the gateway's fallback proxy
  should append the slash itself rather than relay the redirect.
- **Hardening:** the only paths clients legitimately reach on devpi are
  `/pypi/simple/...` on the gateway and the generated file routes
  `/root/pypi/+f/...` (and `/root/pypi/+e/...`, devpi's external-link route).
  The gateway rechecks those file routes against the plugin's internal
  file-policy endpoint before proxying. Other `/root/...` paths are denied at the gateway to keep
  anyone from browsing the unfiltered `root/pypi/+simple/` mirror, bypassing
  constraints, or bypassing the Gitea-first private-name check. Direct file URL
  age policy is also enforced inside the Artea devpi plugin.

First-boot note: the container reports healthy as soon as `/+status` responds, which
can be a second or two before `root/constrained` exists on the very first boot; a
fallback hitting that window gets a devpi 404 and pip simply reports "not found" —
retrying succeeds.

## Constraints management (contract for the policy-sync author)

Constraints are the `constraints` config key (list of requirement lines) on
`root/constrained`. The shared upstream recency gate is `min_upstream_age`, an
ISO 8601 duration such as `P3D` or `PT72H` (`P0D` disables it). The optional
`osv_url` key points at policy-sync's internal `POST /osv/querybatch` endpoint;
when set, the plugin hides/rejects public versions policy-sync reports as OSV
malicious. Only `root` may modify these keys; devpi accepts HTTP Basic
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
      -> {"result": {..., "constraints": [...], "min_upstream_age": "P3D"}}
PATCH http://devpi:3141/root/constrained   Accept: application/json
      Authorization: Basic base64(root:$DEVPI_ROOT_PASSWORD)
      Content-Type: application/json
      body: the full config dict from GET .result with "constraints"
            and "min_upstream_age" replaced by the policy values
```

The `constraints` value may be a list of lines or one raw multi-line string —
the plugin normalizes strings itself, skipping blanks and `#` comments, so
policy-sync can push the `pypi-constraints.txt` content as-is.
Setting `"constraints": []` (or an empty string) restores **allow-all** — that is
the plugin's no-constraints behavior; a single `*` line is default-deny. The
plugin validates `min_upstream_age` when policy-sync PATCHes the index. If
`osv_url` is unset in index config, the plugin falls back to `ARTEA_OSV_URL`.
OSV lookup failures fail open for uncached versions.

## Persistence

| Item | Value |
|------|-------|
| Server/cache dir | `/devpi/server` (`DEVPISERVER_SERVERDIR`, set in the image) |
| Volume | the `artea-devpi-data` PVC → `/devpi/server` |
| Ownership | uid 1000 (`devpi` user); the chart's PVC is initialized with the right ownership |

## Environment variables

| Var | Default | Purpose |
|-----|---------|---------|
| `DEVPI_ROOT_PASSWORD` | *(required)* | sets the root password at first init; used as HTTP Basic auth by `ensure_index.py` and by policy-sync (from the chart's `artea` Secret). Rotating it after init requires changing the root password on the server too, or just wiping the volume |
| `DEVPI_OUTSIDE_URL` | `http://localhost:8080` | public base URL baked into generated links |
| `DEVPI_PORT` | `3141` | listen port (contract value; only override in tests) |
| `DEVPI_STARTUP_TIMEOUT` | `60` | seconds to wait for the server before the entrypoint gives up |
| `DEVPI_ONESHOT` | `0` | `1` = init + ensure index, then exit (used by tests) |
| `ARTEA_OSV_URL` | empty | policy-sync OSV verdict endpoint used when `root/constrained` has no `osv_url` config |

## Version pins

Pinned via build args; the `Dockerfile` is the single source of truth:

```
DEVPI_SERVER_VERSION=6.20.2
```

## Tests

`tests/test_entrypoint.py` runs the real `entrypoint.sh` and `ensure_index.py`
against fake `devpi-init`/`devpi-server` binaries (the fake server really listens
and emulates the index JSON API, so the readiness probe and HTTP calls are
exercised for real). It asserts: init runs only on an empty server dir, the
constrained index is created only when missing (with root credentials and the
fail-closed `*` constraints seed plus `min_upstream_age=P0D` — never overwriting
an existing index), re-runs are no-ops, and a missing `DEVPI_ROOT_PASSWORD`
fails fast. No docker or network needed:

```sh
python3 -m pytest devpi/tests/ -q
```
