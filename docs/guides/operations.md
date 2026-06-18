# Operations: upgrades, backup, revocation, troubleshooting

## Upgrades — the no-fork rule

Artea never vendors or patches upstream source (architecture requirement R7,
[ADR-0004](../adr/0004-upstream-isolation-no-fork.md)). Every component is
either a stock upstream image plus runtime configuration, or our own code.
All version pins live in `.env` (plus `gitea/UPSTREAM`); never use floating
`latest`.

The generic bump procedure for every component:

```sh
# 1. edit the pin in .env (and gitea/UPSTREAM for gitea)
# 2. recreate
make up
# 3. verify
make e2e
```

Per-component notes:

| Component | What it is | Bump notes |
|-----------|------------|------------|
| `gitea` | stock `gitea/gitea` image + generated `gitea/app.ini` + generated `gitea/custom/` template overlay | Read upstream release notes for breaking `app.ini` changes; check that template overrides in `gitea/custom/templates/*.template` still match the upstream templates of the new version (they are version-coupled). `gitea/patches/` is an empty patch queue — if it ever gains patches, follow the apply/bump procedure documented there instead of using the stock image directly. |
| `verdaccio` | stock `verdaccio/verdaccio:6` image + generated config + our plugins | Our auth and filter plugins target the stable Verdaccio plugin API; on a major bump, re-run e2e scenarios S4/S5 (pull-through + policy filter) before rollout. |
| `devpi` | our image (`devpi/Dockerfile`: python-slim + `devpi-server` + Artea devpi policy plugin) | Bump the base image and the `devpi-server` pin in `.env`, rebuild. The server data is a disposable cache (see below) — wiping it on upgrade is safe. |
| `gateway` | stock nginx + our config | Bump the nginx pin; config is plain nginx conf, rarely affected. |
| `policy-sync` | our Python service | Normal release; no upstream to track. |

If an upgrade truly requires changing upstream behavior that config/overlay/
plugins cannot reach, that is an ADR + `gitea/patches/` decision — not an ad-hoc
fork (first expected candidate: PAT expiry dates).

### Client PAT migration

Current package clients must authenticate with a Gitea PAT that has the package
scope plus `read:user` and `read:organization`, and the account must be an
member of the configured namespace org (`ARTEA_NAMESPACE`, default `artea`).
Tokens minted before the org-membership gateway guard may only have
`read:package` or `write:package`; replace those before rollout or installs
through `/npm/` and `/pypi/simple/` will start returning 401.

## Production security caveats

The compose defaults are local-dev conveniences. Before any non-throwaway
deployment, replace every placeholder secret in `.env` (and every
`.Values.secrets.*` value in Helm) before first start. Keep `.env` and
generated credentials out of git.

The gateway is the only public entrypoint. Do not publish or ingress the
internal Gitea, Verdaccio, devpi, or policy-sync ports directly; they assume the
gateway's auth, routing, and precedence checks. In Kubernetes, expose the
gateway Ingress for TLS/host routing only and leave the package routing logic
inside the gateway.

### Base-image digest pins

The images we build ourselves digest-pin their base images — a tag is floating,
a digest is not (R7,
[ADR-0004](../adr/0004-upstream-isolation-no-fork.md)). The tag stays in the
`FROM` line for humans; docker ignores it once a digest is present.

| Image Dockerfile | Base to re-resolve |
|------------------|--------------------|
| `devpi/Dockerfile` | `python:3.14-slim` |
| `policy-sync/Dockerfile` | `python:3.14-slim` |
| `scripts/Dockerfile.bootstrap` | `python:3.14-slim` |
| `deploy/docker/verdaccio-assets/Dockerfile` | `busybox:1.38` |

To bump:

```sh
docker buildx imagetools inspect python:3.14-slim   # copy the "Digest:" line
# update the matching FROM lines, for example:
#   FROM python:3.14-slim@sha256:<new digest>
make up
make e2e
```

## Backup and restore

**The Gitea data volume is the single source of truth.** It contains users,
PATs, org/team membership, all private package artifacts (npm tarballs, Python
wheels/sdists), and the `${ARTEA_NAMESPACE}/registry-policy` repo. Back up only this:

- the `gitea-data` named volume (includes the embedded SQLite DB unless you
  configured an external database — back that up too if so),
- your `.env` (secrets + version pins; it is never committed).

Everything else is disposable and must **not** be in the backup set:

- Verdaccio storage: a cache of npmjs.org; refills on demand.
- devpi data: a mirror/cache of pypi.org; refills on demand, and its
  `root/constrained` index is re-created by devpi init + policy-sync.
- the `policy-data` shared volume: rewritten by policy-sync from the policy
  repo on startup and on every push webhook.

Procedure (cold backup is simplest and the stack tolerates the short gitea
downtime; both targets use a throwaway pinned-alpine container to tar/untar
the named volume):

```sh
make backup
# -> backups/gitea-data-<timestamp>.tar.gz  (./backups/ is gitignored)

make restore BACKUP=backups/gitea-data-<timestamp>.tar.gz
# empties the volume and untars the backup; asks for confirmation first
```

(Alternatively use `gitea dump` inside the container for a hot backup.)

After a restore, caches rebuild themselves; the first installs are slower
while the pull-through caches warm up, and the PyPI cache comes back
**fail-closed** until policy-sync re-syncs (see below). Run `make bootstrap`
afterwards if `e2e/tmp/credentials.env` no longer matches the restored PATs.

### `make clean` vs `make destroy`

| Target | Removes | Keeps |
|--------|---------|-------|
| `make down` | containers | all volumes |
| `make clean` | containers, the disposable cache volumes (`devpi-data`, `verdaccio-storage`, `policy-data`), `e2e/tmp` | **`gitea-data`** |
| `make destroy` | everything, including `gitea-data` | nothing |

`clean` is always safe: `make up && make bootstrap` brings the stack back with
all users, PATs, and private packages intact, and the caches refill on demand.
`destroy` deletes the store of record and therefore prompts interactively (type
the project name, `artea`) — take a `make backup` first if in doubt.

## Policy authoring

Policy is code in the Gitea repo `${ARTEA_NAMESPACE}/registry-policy`, changed
**via pull request** (review, audit history, revert — ADR-0006). The canonical
authoring file is a single unified `policy.toml` (ADR-0007): one cross-ecosystem
schema for npm and PyPI, with ALLOW-WINS precedence resolved by policy-sync's
compiler. Fresh installs are seeded with a default-allow `policy.toml`; the full
schema and validation rules are in
[`docs/policy-schema.md`](../policy-schema.md).

Typical change: open a PR that edits `policy.toml`, e.g. block a package version

```toml
schema = 1

[defaults]
action = "allow"

[[rules]]
ecosystem = "npm"
name = "left-pad"
versions = "1.3.0"
action = "deny"
reason = "example block"
```

After merge, policy-sync compiles `policy.toml` into the per-engine artifacts
(`npm-rules.yaml`, `upstream-policy.yaml`, `pypi-constraints.txt`) the engines
consume — you do **not** edit those by hand. `policy.toml` is the only authoring
input. A `policy.toml` that is absent or fails to parse or compile fails the
sync and **keeps the last-known-good** enforcement in effect (it is never
silently downgraded); the error is logged and `/healthz` reports
`last_sync_ok: false`.

**Verifying a sync.** Check policy-sync health for `last_sync_ok`:

```sh
docker compose exec policy-sync python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8920/healthz').read())"
```

## Fail-closed states and recovery

Both pull-through caches fail **closed** when policy state is missing (R3,
e2e S15):

- **npm — policy file lost** (`policy-data` wiped, e.g. by `make clean`, or
  `/policy/npm-rules.yaml` deleted/corrupted): the tarball middleware answers
  503 and the filter strips packuments to zero versions rather than serving
  unfiltered. A stale-but-valid file keeps serving as last-known-good.
- **PyPI — devpi cache lost** (`devpi-data` wiped): on the next boot the devpi
  entrypoint recreates `root/constrained` seeded with the `*` constraint
  (the constrained-index block-everything sentinel), so a fresh mirror serves
  nothing instead of everything. The seed also sets `min_upstream_age=P0D`
  until policy-sync applies the shared upstream policy.
- **PyPI — policy/metadata unavailable**: public PyPI fallback goes through
  devpi's `root/constrained` index. If `pypi-constraints.txt` has never synced,
  the `*` seed blocks public packages; if an active `upstream-policy.yaml`
  age gate cannot be verified against PyPI JSON upload-time metadata, the
  devpi policy plugin hides/rejects the unverifiable public files rather than
  serving them unfiltered.

Recovery is policy-sync's job and needs no manual steps: it syncs at startup,
on every push webhook of `${ARTEA_NAMESPACE}/registry-policy`, and on a 5-minute fallback
poll, compiling `policy.toml` into `npm-rules.yaml` (picked up by Verdaccio
within the mtime-reload window) and `upstream-policy.yaml`, writing
`pypi-constraints.txt` for debugging, and replacing the seeded `*` constraints
plus `min_upstream_age` in devpi with the real policy. To force immediate recovery:
`docker compose restart policy-sync`
(its startup sync re-applies both files), then check
`docker compose exec policy-sync python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8920/healthz').read())"`
for `last_sync_ok: true`.

One non-self-healing case: if the policy-sync container logs
`ERROR: /policy is not writable`, the `policy-data` volume ownership does not
match the image's non-root user **and** the container was started with a
`user:` override that prevents the entrypoint from repairing it (started as
root, the entrypoint chowns the volume and drops privileges itself). Run the
`chown` command printed in that error, or remove the override.

## PAT revocation

Revoke a token in the Gitea UI (**Settings → Applications → Delete**) or via
`DELETE /api/v1/users/{username}/tokens/{id}` as admin. Propagation:

| Path | Effect |
|------|--------|
| Gitea-direct (twine and private package files after the gateway guard) | immediate inside Gitea; guarded entrypoints may still honor a positive gateway cache for up to 30 s |
| npm pull-through (`/npm/` via gateway + Verdaccio) | within 60 s worst-case — the gateway and Verdaccio each cache only *positive* auth validations for 30 s |
| PyPI paths via the gateway (`/pypi/simple/`, `/root/...`) | within 30 s — the gateway positive auth cache is 30 s |

So the system-wide guarantee is: **a revoked token stops working everywhere
within 60 seconds** (e2e scenario S12). A revoked token appearing to work for a
short time on pull-through package installs is expected, not a bug.

Remember that Okta deactivation does not delete Gitea PATs — see
[okta.md](okta.md#5-the-pat-after-sso-flow).

## Troubleshooting

| Symptom | Likely cause | Fix / check |
|---------|--------------|-------------|
| Everything returns 502 | A backend container is down | `docker compose ps`, `docker compose logs gateway <service>` |
| Package proxy requests return 401 or 403 with a valid-looking token | Token revoked, missing `read:user`/`read:organization`/package scope, non-member of the configured namespace org, or Gitea unreachable from verdaccio/gateway | `curl -u user:PAT http://localhost:8080/api/v1/user`; then `curl -u user:PAT http://localhost:8080/api/v1/orgs/${ARTEA_NAMESPACE}/members/user` (204 means org guard can pass); package-scope probes should return 200 with a JSON package list: `curl -u user:PAT "http://localhost:8080/api/v1/packages/${ARTEA_NAMESPACE}/?type=pypi&limit=1"`; check Gitea logs |
| `npm install @${ARTEA_NAMESPACE}/x` 404s | Package/version not published — the gateway routes `@${ARTEA_NAMESPACE}/*` to Gitea server-side, so missing client scope config is no longer a cause (legacy scope-registry configs still work) | Check the configured namespace org's package list in Gitea; client setup in [clients-npm.md](clients-npm.md) |
| `npm publish` rejected | Read-only cache (unscoped publish), missing `write:package` / `read:user` / `read:organization`, or no org write permission | Scope the package `@${ARTEA_NAMESPACE}/*`; check token scope and org membership |
| Public npm package has missing versions | A `deny` rule in `policy.toml`, or the shared `upstream.min_age` | Intentional; edit `policy.toml` in the policy repo via PR (see [Policy authoring](#policy-authoring)) |
| Policy change has no effect | Webhook not delivered, policy-sync down, or `policy.toml` failed to compile (sync kept last-known-good) | Repo settings → Webhooks → recent deliveries on `${ARTEA_NAMESPACE}/registry-policy`; `docker compose logs policy-sync` (a compile error in `policy.toml` is logged and fails that sync, leaving the previous policy in effect); check `/healthz` for `last_sync_ok`; verify the compiled `/policy/npm-rules.yaml`, `/policy/upstream-policy.yaml`, and `/policy/pypi-constraints.txt` changed as expected; the slow-poll fallback will also catch up eventually |
| `pip install <private>` resolves a public version | Gateway 404-fallback misrouting (or a client `extra-index-url` bypass) | Treat as a security incident if client config is clean: verify the gateway serves Gitea's 200 for `/pypi/simple/<name>/` and only falls back on 404 |
| `pip install <public>` 404s | devpi mirror cold/unreachable, name blocked by `pypi-constraints.txt`, still too new under `upstream-policy.yaml`, or a freshly recreated cache still fail-closed (`*` seed) | `docker compose logs devpi` and `policy-sync`; check the policy files in the policy repo; check policy-sync `/healthz` for `last_sync_ok` |
| npm/pip download URLs point at the wrong host | Gitea `ROOT_URL` misconfigured | Must be exactly `http://localhost:8080/` (the public gateway URL) so generated file URLs resolve through the gateway |
| Revoked PAT still works on installs | 30 s positive-auth caches in the gateway and Verdaccio; worst-case remains within 60 s | Expected; see above |
| Pull-through is slow / disk is filling | Cache volumes grow unbounded | Safe to wipe: `make clean && make up && make bootstrap` (caches refill; PyPI comes back fail-closed until policy-sync syncs) |
| `twine upload` 409 Conflict | Re-uploading an already-uploaded file | Bump the version; Gitea package files are immutable |
