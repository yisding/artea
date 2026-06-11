# Artea v1 Architecture

Artea is an open-source private package registry with pull-through caching of public
registries — an open-source alternative to Artifactory. v1 supports **npm (JS/TS)** and
**PyPI (Python)** only, but every design decision below is made so additional formats
(Maven, RPM, Debian, containers, ...) can be added without rework.

This document is the canonical design contract. All components must conform to it.
If implementation reality forces a deviation, document it in `docs/adr/`.

## Hard requirements

| ID | Requirement |
|----|-------------|
| R1 | Unified auth across all registries; SSO via Okta/OIDC for humans |
| R2 | Private publish + pull-through of public packages; private names take absolute precedence |
| R3 | Ability to block public packages and specific versions from pull-through |
| R4 | Standard Python (pip/uv/poetry/twine) and JS (npm/pnpm/yarn) tooling works unmodified |
| R5 | Long-lived credentials (up to ~1 year) for pull and publish |
| R6 | The same credential works for both publishing and downloading |
| R7 | Upstream isolation: we must be able to pull improvements from upstream Gitea, Verdaccio, and devpi indefinitely |

## Component topology

One public entrypoint (the gateway). Everything else is internal.

```
                          ┌─────────────────────────────────────────────┐
   client tools ────────▶ │  gateway (nginx)  :8080  — single public URL │
                          └──┬──────────────┬──────────────┬────────────┘
                             │              │              │ 404-fallback (pypi only)
                  ┌──────────▼───┐  ┌───────▼──────┐  ┌────▼─────────┐
                  │ gitea :3000  │  │ verdaccio    │  │ devpi :3141  │
                  │ identity,    │  │ :4873        │  │ PyPI pull-   │
                  │ PATs, private│  │ npm pull-    │  │ through cache│
                  │ packages, UI │  │ through cache│  │ + constraints│
                  └──────▲───────┘  └───────▲──────┘  └────▲─────────┘
                         │ webhook          │ policy file  │ constraints push
                       ┌─┴──────────────────┴──────────────┴─┐
                       │            policy-sync :8920          │
                       └────────────────────────────────────────┘
```

### Fixed contracts (do not change without updating this doc)

| Item | Value |
|------|-------|
| Public base URL (dev) | `http://localhost:8080` |
| Gateway container/port | `gateway`, listens on 80, published as 8080 |
| Gitea container/port | `gitea`, 3000 (stock `gitea/gitea` image, exact tag pinned in `.env`) |
| Verdaccio container/port | `verdaccio`, 4873 (stock `verdaccio/verdaccio:6` image, exact tag pinned) |
| devpi container/port | `devpi`, 3141 (our `devpi/Dockerfile`: python-slim + devpi-server + devpi-constrained) |
| policy-sync container/port | `policy-sync`, 8920 (our `policy-sync/` Python service) |
| Private namespace org | `artea` (Gitea organization; npm scope `@artea`) |
| Policy repo | Gitea repo `artea/registry-policy` containing `npm-rules.yaml`, `pypi-constraints.txt` |
| Shared policy volume | named volume `policy-data`, mounted at `/policy` in verdaccio and policy-sync |
| Bootstrap admin | `artea-admin`, password from `.env` (`ARTEA_ADMIN_PASSWORD`) |
| Env file | `.env` at repo root (`.env.example` committed); all version pins live here |
| devpi indexes | `root/pypi` (mirror of pypi.org), `root/constrained` (type=constrained, bases=root/pypi) |

### Gitea endpoint paths (verified against upstream source)

- npm: `/api/packages/{owner}/npm/` (publish = PUT by npm client to the same registry URL)
- pypi upload (twine): `POST /api/packages/{owner}/pypi/`
- pypi simple index: `GET /api/packages/{owner}/pypi/simple/{name}` (PEP 503)
- pypi files: `GET /api/packages/{owner}/pypi/files/{name}/{version}/{filename}`
- auth check: `GET /api/v1/user` (accepts Basic `user:PAT` and `Authorization: token <PAT>`)
- orgs/teams for group mapping: `GET /api/v1/user/orgs`, `GET /api/v1/user/teams`

Gitea must run with `ROOT_URL = http://localhost:8080/` so generated tarball/file URLs
resolve through the gateway.

## Resolution flows

### npm — precedence by scope (no merging anywhere)

Client `.npmrc` (this is the documented client contract):

```ini
registry=http://localhost:8080/npm/
@artea:registry=http://localhost:8080/api/packages/artea/npm/
//localhost:8080/npm/:_auth=<base64 user:PAT>
//localhost:8080/api/packages/artea/npm/:_authToken=<PAT>
always-auth=true
```

- `@artea/*` → routed by the npm client itself straight to Gitea. Publish and install
  use the same URL, same token (R6). Verdaccio never sees private packages.
- Everything else → Verdaccio: pull-through cache of npmjs.org with the policy filter
  applied (R2, R3). Verdaccio is **read-only**: publish is denied in its config —
  the only writable surface is Gitea.
- Defense in depth: Verdaccio config also denies access/proxy for `@artea/*` so a
  misconfigured client can never leak private names to npmjs.

### Python — precedence by gateway 404-fallback (PEP 503 has no scopes)

Client config: single index URL `http://localhost:8080/pypi/simple/`, credentials via
netrc/keyring (`machine localhost login <user> password <PAT>`).

Gateway logic for `GET /pypi/simple/{name}/`:
1. Proxy to Gitea `/api/packages/artea/pypi/simple/{name}`.
2. If Gitea returns **200** → serve it. Done. PyPI is never consulted for this name —
   this is the dependency-confusion guarantee (R2): a private name fully shadows public.
3. If Gitea returns **404** → nginx `proxy_intercept_errors` + named fallback location →
   proxy to devpi `root/constrained` index (PyPI mirror filtered by constraints, R3).
   Only a true 404 falls through; 401/403 short-circuit and never reach the public
   mirror.

The gateway PEP 503-normalizes the project name (lowercase, collapse `[-_.]+` to `-`)
before the Gitea-first lookup, so non-canonical spellings cannot dodge the private
shadow, and it appends the trailing slash itself so the fallback never relies on a
devpi redirect that would skip the precedence check.

- Publishes: `twine upload` → `https://host/api/packages/artea/pypi/` (Gitea direct).
  **Artifacts/wheels live in Gitea**, never in devpi. devpi is a disposable cache.
- devpi file URLs (`/root/...`) are routed by the gateway to devpi; Gitea file URLs
  (`/api/packages/...`) route to Gitea.
- devpi has no auth of its own and is not exposed; the gateway enforces auth on all
  devpi-bound paths via nginx `auth_request` subrequests to Gitea `/api/v1/user`,
  forwarding the client's Authorization header (R1).

## Auth model

- **Humans**: SSO into Gitea via an OIDC authentication source (Okta works; documented
  in `docs/guides/okta.md`). Self-registration disabled.
- **Tools**: HTTP Basic with `username:Gitea-PAT` everywhere (npm also supports token
  auth on Gitea paths). PATs are created in Gitea, currently non-expiring (satisfies
  R5; an expiry patch is a planned v2 fork patch), scoped `read:package` or
  `write:package` (Gitea's actual scope strings are singular; write implies read →
  R6). PATs must additionally carry `read:user` — the gateway's `auth_request`
  guard calls `GET /api/v1/user`, which requires it — and `read:organization` so
  Verdaccio's org→group mapping works.
- **Verdaccio**: our auth plugin validates each request's Basic credential against
  Gitea `/api/v1/user`, maps Gitea org/team membership to Verdaccio groups, caches
  positive results for 30s (matching the gateway's auth_request cache, so PAT
  revocation takes effect comfortably within the 60s budget of S12).
- **devpi**: no plugin needed — the gateway's `auth_request` guard covers it.
- **Anonymous access**: none, anywhere. The gateway's `auth_request` guard covers
  `/npm/` as well as the devpi paths, so Verdaccio's service endpoints (`/-/ping`,
  search, audit) are not reachable anonymously either.

## Policy model (R3)

Policy-as-code in the Gitea repo `artea/registry-policy`:

- `npm-rules.yaml` — blocked package names, scopes, and semver ranges. Consumed by our
  Verdaccio **filter plugin**, which re-reads the file from `/policy` when its mtime
  changes (no restart needed).
- `pypi-constraints.txt` — devpi-constrained format (pip-constraints-like; supports
  `name<2`, `name ==1.2.3`, and `*` default-deny). Applied by policy-sync to the
  `root/constrained` index.

`policy-sync` receives a Gitea push webhook (plus a startup sync and a slow poll as
fallback), fetches the two files via Gitea's raw-content API using a dedicated
low-privilege service account PAT (`svc-policy`, read-only on the policy repo),
writes `npm-rules.yaml` into the shared `/policy` volume, and pushes the constraints
into devpi.

**Enforcement depth (npm):** the filter plugin filters packuments (metadata) AND the
same package registers a Verdaccio middleware that rejects tarball downloads
(`GET .../-/<file>.tgz`) of blocked versions with 403 — blocks cannot be bypassed by
constructing the tarball URL directly.

**Failure mode is fail-closed:** if `/policy/npm-rules.yaml` is missing or unparsable,
the middleware rejects tarball downloads with 503 and the filter serves packuments
with zero versions (Verdaccio swallows filter exceptions, so stripping every version
is the fail-closed packument shape) rather than silently allowing everything; a
stale-but-valid file keeps serving as last-known-good. On the
Python side, the devpi entrypoint seeds a freshly created `root/constrained` index
with the `*` constraint (block everything) so a wiped cache volume is closed until
policy-sync's next successful sync replaces it with the real policy.

**Governance:** policy changes go through PRs and are enforceably auditable:
`registry-policy`'s default branch carries branch protection (no direct pushes except
the admin allowlist, ≥1 required approval), and developers are members of a
`developers` team (packages write, code write for PR branches) — never org Owners.

## Upstream isolation (R7) — the no-fork rule

1. **Gitea runs the stock upstream Docker image.** All customization is runtime
   overlay: `gitea/app.ini` (mounted config) and `gitea/custom/` (Gitea's supported
   `custom/` directory: template/asset overrides). No source patches in v1.
2. **Verdaccio and devpi are consumed as released artifacts.** Our code is plugins
   against their stable plugin APIs (`verdaccio/plugins/*` as npm packages; devpi
   needs none in v1).
3. **`gitea/patches/`** is an empty quilt-style patch queue with an apply script and a
   documented bump procedure — the escape hatch for the day we need a source patch
   (first candidate: PAT expiry dates). Until then, upgrades = bump pin in `.env`,
   `make up`, `make e2e`.
4. All version pins live in `.env` / `gitea/UPSTREAM`. Never use floating `latest`
   in committed files; our own Dockerfiles digest-pin their base images.
5. Prefer Gitea's injection extension points (`custom/templates/custom/header.tmpl`
   etc.) over full template copies — a copied core template must be re-verified on
   every upstream bump and is a last resort.

## Hiding git-hosting features

Config-first, in `gitea/app.ini`: disable registration, disable repo units
(issues/PRs/wiki/projects/actions/releases) by default, landing page → packages
UI, disable migrations/mirrors, disable RSS/federation surface. Template overlay in
`gitea/custom/templates/` de-gits the navbar and home page. What cannot be hidden
without source patches gets documented in `gitea/README.md` and deferred — repos
must remain functional anyway for `registry-policy`.

## Kubernetes deployment (`deploy/helm/artea`)

The compose stack is the dev/reference deployment; Kubernetes is the production
shape. R7 extends to deployment artifacts: **reuse official upstream charts**.

- **Umbrella Helm chart** at `deploy/helm/artea`: dependencies = the official Gitea
  chart and the official Verdaccio chart; our own templates exist ONLY for devpi,
  policy-sync, the gateway, and the bootstrap Job/RBAC. The stock Verdaccio *image*
  is mandatory; if the official chart's values cannot deliver our plugins/config, the
  documented fallback is a minimal in-house Deployment template using the stock image
  plus an initContainer that copies built plugins from an assets image.
- **Gateway stays the routing brain**: a stock-nginx Deployment with the existing
  `nginx.conf` + njs delivered via ConfigMap (upstream hosts parametrized to cluster
  DNS names). An Ingress in front does TLS and host routing ONLY — `auth_request`,
  the Gitea-first 404-fallback, and PEP 503 normalization are never ported into
  ingress annotations. Single public base URL preserved.
- **Policy delivery over HTTP in K8s** (no shared volume, no RWX): the filter plugin
  supports `policy_url` as a first-class alternative to the file — it polls
  policy-sync's `GET /policy/npm-rules.yaml` every 10s with ETag, keeps
  last-known-good in memory, and fails closed after a grace window of persistent
  failure. policy-sync serves that endpoint (cluster-internal). Compose keeps file
  mode; both modes are tested.
- **Bootstrap as a Helm hook Job**: same `scripts/bootstrap.sh` logic and idempotency
  contract, with a token-sink abstraction — `env-file` mode (compose) vs `k8s-secret`
  mode (patches the policy-sync Secret via the API under a namespace-scoped Role and
  triggers a rollout). Runs post-install and post-upgrade.
- **State**: gitea-data PVC is the only store of record; devpi/verdaccio cache PVCs
  are safe to delete (the fail-closed seed makes cache loss benign). Default
  `replicas: 1` everywhere except the stateless gateway.
- **Images**: `ghcr.io/yisding/artea-{devpi,policy-sync,bootstrap,verdaccio-assets}`,
  digest-pinned in values; CI builds/pushes them. Locally, colima's docker-runtime
  k3s sees locally-built images directly.
- **Local dev contract**: `colima start --kubernetes` (k3s), then
  `kubectl port-forward svc/<gateway> 8080:80` — the e2e suite only knows BASE_URL,
  so S1–S16 run unchanged against compose or K8s.
- **CI**: GitHub Actions — GHCR image builds, plus a kind job that helm-installs the
  chart and runs the full e2e suite against it.

## Scale-out design (beyond v1 — do not implement, do not preclude)

- **New format recipe**: (1) private publish = enable Gitea's existing endpoint for
  that format under the org namespace; (2) pull-through = gateway 404-fallback to a
  mature per-format cache where one exists, else native pull-through in Gitea (v2);
  (3) a policy file section + policy-sync adapter. Artifacts always live in Gitea.
- **v2**: native pull-through + policy inside Gitea's package routers (fork patches or
  upstreamed), retiring Verdaccio/devpi one format at a time. The gateway contract
  (one URL, same auth) never changes for clients.
- **v3**: extract the registry into a standalone service with Gitea-issued scoped JWTs
  (GitLab container-registry model).
- Multi-org Python namespacing (PyPI has no scopes) is explicitly deferred; v1 is
  single-org (`artea`).

## Requirements traceability

| Req | Mechanism | E2E scenario |
|-----|-----------|--------------|
| R1 | Gitea OIDC (Okta) + plugins/auth_request validate everything against Gitea | S11, S12 (+ docs) |
| R2 | npm scopes → Gitea; pypi gateway 404-fallback (200 = never consult public) | S2–S4, S6–S9 |
| R3 | Verdaccio filter plugin + tarball middleware + devpi-constrained, fail-closed | S5, S10, S13, S15 |
| R4 | Stock protocols: npm/pnpm/yarn vs Verdaccio+Gitea; pip/uv/twine vs gateway+Gitea | S2–S10 |
| R5 | Gitea PATs (non-expiring today) | S11 |
| R6 | One PAT publishes (Gitea) and pulls (everywhere) | S2/S3, S6/S7, S11 |
| R7 | Stock images + runtime overlays + plugins + patch-queue escape hatch | audit |

## E2E scenarios (the definition of done for v1)

S1 bootstrap: stack up; admin, org `artea`, PAT, policy repo seeded, webhook wired.
S2 `npm publish` `@artea/hello-artea` with PAT → 201 in Gitea.
S3 `npm install @artea/hello-artea` resolves from Gitea via scope routing.
S4 `npm install left-pad` resolves via Verdaccio pull-through from npmjs.
S5 block a `left-pad` version in `npm-rules.yaml`, push, verify it disappears from `npm view left-pad versions`.
S6 `twine upload` a locally built `artea-hello` wheel → Gitea (artifact stored in Gitea).
S7 `pip install artea-hello` via the gateway index.
S8 `pip install six` via gateway → devpi → PyPI pull-through.
S9 precedence: privately publish a name that also exists on PyPI; `pip index versions`
   through the gateway must show ONLY the private versions (proves shadowing).
S10 add `urllib3<2` to constraints, push, verify pip resolves only <2 through the gateway.
S11 same-token: all scenarios above run with one PAT; a `read:package` PAT can
    install but is rejected (Gitea answers 401, not 403) on publish.
S12 revocation: delete the PAT; installs fail within 60s.
S13 tarball enforcement: with a `left-pad` version blocked, a direct authenticated GET
    of that version's tarball URL via /npm/ returns 403 (metadata filtering alone is
    not enough).
S14 governance: a developer PAT (even with repo scopes) cannot push to
    `registry-policy@main` directly; the PR + approval path is required.
S15 fail-closed: with `/policy/npm-rules.yaml` removed (simulated policy-sync outage),
    public npm fetches are rejected rather than served unfiltered (tarballs 503,
    packuments stripped to zero versions); restoring the file recovers within the
    mtime-reload window. A freshly wiped devpi volume serves nothing from the mirror
    until policy-sync syncs.
S16 normalization: non-canonical spellings of a private name (`Artea-Hello`,
    `artea_hello`) through the gateway still resolve to the private package, never
    falling through to the public mirror.
