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
| `gitea` | stock `gitea/gitea` image + mounted `gitea/app.ini` + `gitea/custom/` overlay | Read upstream release notes for breaking `app.ini` changes; check that template overrides in `gitea/custom/templates/` still match the upstream templates of the new version (they are version-coupled). `gitea/patches/` is an empty patch queue — if it ever gains patches, follow the apply/bump procedure documented there instead of using the stock image directly. |
| `verdaccio` | stock `verdaccio/verdaccio:6` image + `verdaccio/config` + our plugins | Our auth and filter plugins target the stable Verdaccio plugin API; on a major bump, re-run e2e scenarios S4/S5 (pull-through + policy filter) before rollout. |
| `devpi` | our image (`devpi/Dockerfile`: python-slim + `devpi-server` + `devpi-constrained`) | Bump the base image and the two package pins in `.env`, rebuild. The server data is a disposable cache (see below) — wiping it on upgrade is safe. |
| `gateway` | stock nginx + our config | Bump the nginx pin; config is plain nginx conf, rarely affected. |
| `policy-sync` | our Python service | Normal release; no upstream to track. |

If an upgrade truly requires changing upstream behavior that config/overlay/
plugins cannot reach, that is an ADR + `gitea/patches/` decision — not an ad-hoc
fork (first expected candidate: PAT expiry dates).

### Base-image digest pins

The two images we build ourselves (`devpi/Dockerfile`, `policy-sync/Dockerfile`)
digest-pin their `python:3.12-slim` base — a tag is floating, a digest is not
(R7, [ADR-0004](../adr/0004-upstream-isolation-no-fork.md)). The tag stays in
the `FROM` line for humans; docker ignores it once a digest is present. To bump:

```sh
docker buildx imagetools inspect python:3.12-slim   # copy the "Digest:" line
# update the FROM line in devpi/Dockerfile and policy-sync/Dockerfile:
#   FROM python:3.12-slim@sha256:<new digest>
make up    # rebuilds both images
make e2e
```

## Backup and restore

**The Gitea data volume is the single source of truth.** It contains users,
PATs, org/team membership, all private package artifacts (npm tarballs, Python
wheels/sdists), and the `artea/registry-policy` repo. Back up only this:

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

## Fail-closed states and recovery

Both pull-through caches fail **closed** when policy state is missing (R3,
e2e S15):

- **npm — policy file lost** (`policy-data` wiped, e.g. by `make clean`, or
  `/policy/npm-rules.yaml` deleted/corrupted): the tarball middleware answers
  503 and the filter strips packuments to zero versions rather than serving
  unfiltered. A stale-but-valid file keeps serving as last-known-good.
- **PyPI — devpi cache lost** (`devpi-data` wiped): on the next boot the devpi
  entrypoint recreates `root/constrained` seeded with the `*` constraint
  (devpi-constrained's block-everything sentinel), so a fresh mirror serves
  nothing instead of everything.

Recovery is policy-sync's job and needs no manual steps: it syncs at startup,
on every push webhook of `artea/registry-policy`, and on a 5-minute fallback
poll, rewriting `npm-rules.yaml` (picked up by Verdaccio within the
mtime-reload window) and replacing the seeded `*` constraints with the real
policy. To force immediate recovery: `docker compose restart policy-sync`
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
| Gitea-direct (npm `@artea/*` — gateway scope route under `/npm/` or legacy `/api/packages/...`; twine; private pip files) | immediate |
| public npm pull-through (non-`@artea` `/npm/` via Verdaccio) | ≤ 60 s — the Verdaccio auth plugin caches *positive* validations for 60 s |
| PyPI paths via the gateway (`/pypi/simple/`, `/root/...`) | per-request `auth_request` against Gitea — effectively immediate |

So the system-wide guarantee is: **a revoked token stops working everywhere
within 60 seconds** (e2e scenario S12). A revoked token appearing to work for
under a minute on `npm install` of public packages is expected, not a bug.

Remember that Okta deactivation does not delete Gitea PATs — see
[okta.md](okta.md#5-the-pat-after-sso-flow).

## Troubleshooting

| Symptom | Likely cause | Fix / check |
|---------|--------------|-------------|
| Everything returns 502 | A backend container is down | `docker compose ps`, `docker compose logs gateway <service>` |
| All requests 401 with valid token | Token revoked, or Gitea unreachable from verdaccio/gateway (auth validation goes to Gitea) | `curl -u user:PAT http://localhost:8080/api/v1/user`; check gitea logs |
| `npm install @artea/x` 404s | Package/version not published — the gateway routes `@artea/*` to Gitea server-side, so missing client scope config is no longer a cause (legacy `@artea:registry` configs still work) | Check the `artea` org's package list in Gitea; client setup in [clients-npm.md](clients-npm.md) |
| `npm publish` rejected | Read-only cache (unscoped publish), missing `write:package`, or no org write permission | Scope the package `@artea/*`; check token scope and org membership |
| Public npm package has missing versions | Policy block in `npm-rules.yaml` | Intentional; edit the policy repo via PR |
| Policy change has no effect | Webhook not delivered, or policy-sync down | Repo settings → Webhooks → recent deliveries on `artea/registry-policy`; `docker compose logs policy-sync`; verify `/policy/npm-rules.yaml` mtime changed in the verdaccio container; the slow-poll fallback will also catch up eventually |
| `pip install <private>` resolves a public version | Gateway 404-fallback misrouting (or a client `extra-index-url` bypass) | Treat as a security incident if client config is clean: verify the gateway serves Gitea's 200 for `/pypi/simple/<name>/` and only falls back on 404 |
| `pip install <public>` 404s | devpi mirror cold/unreachable, name blocked by `pypi-constraints.txt`, or a freshly recreated cache still fail-closed (`*` seed) | `docker compose logs devpi`; check the constraints file in the policy repo; check policy-sync `/healthz` for `last_sync_ok` |
| npm/pip download URLs point at the wrong host | Gitea `ROOT_URL` misconfigured | Must be exactly `http://localhost:8080/` (the public gateway URL) so generated file URLs resolve through the gateway |
| Revoked PAT still works on npm installs | 60 s positive-auth cache in Verdaccio | Expected; see above |
| Pull-through is slow / disk is filling | Cache volumes grow unbounded | Safe to wipe: `make clean && make up && make bootstrap` (caches refill; PyPI comes back fail-closed until policy-sync syncs) |
| `twine upload` 409 Conflict | Re-uploading an already-uploaded file | Bump the version; Gitea package files are immutable |
