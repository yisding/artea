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
| R3 | Ability to block public packages and specific versions from pull-through, including recency gates |
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
                  │ packages, UI │  │ through cache│  │ + policy     │
                  └──────▲───────┘  └───────▲──────┘  └────▲─────────┘
                         │ webhook          │ policy files │ index config
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
| devpi container/port | `devpi`, 3141 (our `devpi/Dockerfile`: python-slim + devpi-server + Artea devpi policy plugin) |
| policy-sync container/port | `policy-sync`, 8920 (our `policy-sync/` Python service) |
| Private namespace org | `ARTEA_NAMESPACE` (default `artea`; Gitea organization and npm scope `@${ARTEA_NAMESPACE}`) |
| Policy repo | Gitea repo `${ARTEA_NAMESPACE}/registry-policy` containing `npm-rules.yaml`, `upstream-policy.yaml`, `pypi-constraints.txt` |
| Shared policy volume | named volume `policy-data`, mounted at `/policy` in verdaccio and policy-sync |
| Bootstrap admin | `ARTEA_ADMIN_USER` (default `${ARTEA_NAMESPACE}-admin` when unset), password from `.env` (`ARTEA_ADMIN_PASSWORD`) |
| Env file | `.env` at repo root (`.env.example` committed); all version pins, secrets, and namespace settings live here |
| Runtime configs | rendered from `*.template` files into `.generated/` by `make render-configs` / `make up` |
| devpi indexes | `root/pypi` (mirror of pypi.org), `root/constrained` (type=constrained, bases=root/pypi) |

### Gitea endpoint paths (verified against upstream source)

- npm: `/api/packages/${ARTEA_NAMESPACE}/npm/` (publish = PUT by npm client to the same registry URL)
- pypi upload (twine): `POST /api/packages/${ARTEA_NAMESPACE}/pypi/`
- pypi simple index: `GET /api/packages/${ARTEA_NAMESPACE}/pypi/simple/{name}` (PEP 503)
- pypi files: `GET /api/packages/${ARTEA_NAMESPACE}/pypi/files/{name}/{version}/{filename}`
- user auth check: `GET /api/v1/user` (accepts Basic `user:PAT` and `Authorization: token <PAT>`)
- orgs/teams for group mapping: `GET /api/v1/user/orgs`, `GET /api/v1/user/teams`
- org membership guard: `GET /api/v1/orgs/{org}/members/{username}` (gateway
  guard for package proxy paths; 2xx = authenticated org member)

Gitea must run with `ROOT_URL = http://localhost:8080/` so generated tarball/file URLs
resolve through the gateway.

## Resolution flows

### npm — precedence by scope, enforced by the gateway (no merging anywhere)

Client `.npmrc` (this is the documented client contract — one registry URL, one
credential value; consumers need only the host-rooted `_auth` line, publishers
add the `/npm/`-scoped copy — the full form, as in
`docs/guides/clients-npm.md`):

```ini
registry=http://localhost:8080/npm/
//localhost:8080/:_auth=<base64 user:PAT>
//localhost:8080/npm/:_auth=<base64 user:PAT>
always-auth=true
```

The same `_auth` value appears on two nerf-dart lines: the host-rooted
`//localhost:8080/:` line covers the tarball URLs Gitea generates from `ROOT_URL`
(`/api/packages/${ARTEA_NAMESPACE}/npm/...` — npm's nerf-dart prefix matching walks up to the
host root); the `//localhost:8080/npm/:` line exists only because `npm publish`
runs a local credential preflight against the registry's *exact* nerf-dart and
never walks up (verified npm 11; see the CLIENT CAVEAT in `gateway/nginx.conf.template`
and `docs/guides/clients-npm.md`).

- `@${ARTEA_NAMESPACE}/*` → routed to Gitea **by the gateway**: a regex location peels
  `/npm/@${ARTEA_NAMESPACE}/...` and the dist-tag API
  `/npm/-/package/@${ARTEA_NAMESPACE}/...` off the Verdaccio route and proxies
  them to `/api/packages/${ARTEA_NAMESPACE}/npm/...`. This is a
  scope match, never a 404-fallback — a 404-fallback would reintroduce
  dependency confusion, while the scope match keeps private-scope names structurally
  unable to reach Verdaccio or npmjs (an unpublished private name 404s, full
  stop). The match is case-insensitive, so case-variant spellings of the scope
  (for example `@ACME/...` when `ARTEA_NAMESPACE=acme`) also route to Gitea
  (and 404 there) instead of reaching
  Verdaccio. The location explicitly matches literal and encoded `@` / scope
  separators because nginx keeps npm's `%2f` publish/packument separator encoded
  during location matching. The forwarded path is derived from the raw
  `$request_uri` via an nginx `map`, so the `%2f`/`%40` encodings npm sends in
  packument/publish URLs reach Gitea byte-for-byte; a request that enters the
  scoped location but whose raw form matches neither map pattern is rejected
  with 400 instead of falling through.
  (Double-encoded separators such as `%252f` also fail the raw-map match and
  are rejected with 400.) The gateway guard runs before Gitea, so non-org users
  and PATs without package scope fail before any Gitea package 404 can fall
  through or obscure authorization. Gitea remains the final package-specific
  permission check. Publish and install use the same URL, same token (R6).
- Everything else under `/npm/` → Verdaccio: pull-through cache of npmjs.org with
  the policy filter applied (R2, R3). Verdaccio is **read-only**: publish is
  denied in its config — the only writable surface is Gitea.
- Legacy client config (optional): the previous two-registry `.npmrc`
  (`@${ARTEA_NAMESPACE}:registry=http://localhost:8080/api/packages/${ARTEA_NAMESPACE}/npm/` plus
  per-registry credential lines) keeps working unchanged — client scope routing
  reaches Gitea directly without exercising the gateway's scope match (S17).
- Defense in depth: Verdaccio config also denies access/proxy for `@${ARTEA_NAMESPACE}/*`, so
  even if a scoped request ever reached it, private names could never leak to
  npmjs.

### Python — precedence by gateway 404-fallback (PEP 503 has no scopes)

Client config: single index URL `http://localhost:8080/pypi/simple/`, credentials via
netrc/keyring (`machine localhost login <user> password <PAT>`).

Gateway logic for `GET /pypi/simple/{name}/`:
1. Proxy to Gitea `/api/packages/${ARTEA_NAMESPACE}/pypi/simple/{name}`.
2. If Gitea returns **200** → serve it. Done. PyPI is never consulted for this name —
   this is the dependency-confusion guarantee (R2): a private name fully shadows public.
3. If Gitea returns **404** → nginx `proxy_intercept_errors` + named fallback location →
   proxy to devpi's `root/constrained` simple page (PyPI mirror filtered by
   constraints and upstream age policy, R3). Only a true 404 falls through;
   401/403 short-circuit and never reach the public mirror.

The gateway PEP 503-normalizes the project name (lowercase, collapse `[-_.]+` to `-`)
before the Gitea-first lookup, so non-canonical spellings cannot dodge the private
shadow, and it appends the trailing slash itself so the fallback never relies on a
devpi redirect that would skip the precedence check.

- Publishes: `twine upload` → `https://host/api/packages/${ARTEA_NAMESPACE}/pypi/` (Gitea direct).
  **Artifacts/wheels live in Gitea**, never in devpi. devpi is a disposable cache.
- Current devpi file URLs (`/root/pypi/+(f|e)/...`) are routed by the gateway
  to devpi only after an internal Artea devpi policy endpoint confirms the
  mirror file is still allowed by the current constrained-index policy; stale
  file URLs for newly blocked versions get 403 without nginx buffering and
  scanning large simple pages. Gitea package routes are limited at the gateway to
  `/api/packages/${ARTEA_NAMESPACE}/npm/` and
  `/api/packages/${ARTEA_NAMESPACE}/pypi/`; those direct package routes use the
  same gateway org/package-scope guard before Gitea's package-specific
  read/write checks. Other owners and non-v1 package formats get 404. The
  Artea devpi policy plugin also guards direct public mirror files when age
  verification is required.
- devpi has no auth of its own and is not exposed; the gateway enforces auth on all
  devpi-bound paths via nginx `auth_request`/njs subrequests to Gitea's user API,
  `${ARTEA_NAMESPACE}` org membership check, and package-scope probe, forwarding the client's
  Authorization header (R1). Client-reachable devpi paths are limited to
  file/external-link routes generated from constrained simple pages; raw
  `/root/*/+simple/` browsing is not exposed.

## Auth model

- **Humans**: SSO into Gitea via an OIDC authentication source (Okta works; documented
  in `docs/guides/okta.md`). Self-registration disabled.
- **Tools**: HTTP Basic with `username:Gitea-PAT` everywhere (npm also supports token
  auth on Gitea paths). PATs are created in Gitea, currently non-expiring (satisfies
  R5; an expiry patch is a planned v2 fork patch), scoped `read:package` or
  `write:package` (Gitea's actual scope strings are singular; write implies read →
  R6). PATs must additionally carry `read:user` for Verdaccio's user validation and
  the gateway's credential-to-login lookup, and `read:organization` for the
  gateway's org-membership guard plus Verdaccio's configured namespace
  org/team→group mapping.
  The gateway also probes Gitea's package-management API
  (`GET /api/v1/packages/${ARTEA_NAMESPACE}/?type=pypi&limit=1`) so package
  proxy and direct package API routes fail closed for PATs missing package
  scope without relying on "package not found" responses.
- **Verdaccio**: our auth plugin validates each request's Basic credential against
  Gitea `/api/v1/user`, maps configured namespace org/team membership to
  Verdaccio groups, caches positive results for 30s (matching the gateway's
  auth_request cache, so PAT revocation takes effect comfortably within the 60s
  budget of S12).
- **devpi**: no plugin needed — the gateway's `auth_request` guard covers it.
- **Anonymous access**: none, anywhere. The gateway's `auth_request` guard covers
  Verdaccio-bound `/npm/` paths as well as the devpi paths, so Verdaccio's service
  endpoints (`/-/ping`, search, audit) are not reachable anonymously either.
  Gitea-bound paths — including private-scope traffic the gateway peels off `/npm/` —
  carry no gateway guard because Gitea enforces its own auth.

## Policy model (R3)

Policy-as-code in the Gitea repo `${ARTEA_NAMESPACE}/registry-policy`:

- `npm-rules.yaml` — blocked npm package names, scopes, and semver ranges.
  Consumed by our Verdaccio **filter plugin**, which re-reads the file from
  `/policy` when its mtime changes (no restart needed).
- `upstream-policy.yaml` — pull-through policy shared by all public upstream
  caches. v1 defines `upstream.min_age` as an ISO 8601 duration such as `P3D`
  or `PT72H`; `P0D` disables the gate. npm and PyPI consume the same value, and
  future formats must do the same rather than inventing per-format age knobs.
- `pypi-constraints.txt` — devpi-constrained-compatible format
  (pip-constraints-like; supports `name<2`, `name ==1.2.3`, and `*`
  default-deny). Applied by policy-sync to the `root/constrained` index.

`policy-sync` receives a Gitea push webhook (plus a startup sync and a slow poll as
fallback), fetches the three files via Gitea's raw-content API using a dedicated
low-privilege service account PAT (`svc-policy`, read-only on the policy repo),
writes `npm-rules.yaml`, `upstream-policy.yaml`, and `pypi-constraints.txt` into
the shared `/policy` volume when present, serves the npm and upstream policies
over HTTP for Kubernetes, and pushes the PyPI constraints plus
`min_upstream_age` into devpi.

**Enforcement depth (npm):** the filter plugin filters packuments (metadata) AND the
same package registers a Verdaccio middleware that rejects tarball downloads
(`GET .../-/<file>.tgz`) of blocked or too-new versions with 403 — blocks cannot
be bypassed by constructing the tarball URL directly. For cold direct tarball
requests under an active age gate, the middleware fetches npm registry metadata
for publish timestamps and fails closed if it cannot verify the version age.

**Enforcement depth (PyPI):** Artea's devpi policy plugin keeps the
`type=constrained` index contract and filters the public simple index by version
constraints and upstream upload time. It also guards direct `root/pypi` public
file URLs inside devpi; when an age gate is active, a file without known
project/upload-time context is rejected rather than served unverified.

**Failure mode is fail-closed:** if `/policy/npm-rules.yaml` or
`/policy/upstream-policy.yaml` is missing or unparsable,
the middleware rejects tarball downloads with 503 and the filter serves packuments
with zero versions (Verdaccio swallows filter exceptions, so stripping every version
is the fail-closed packument shape) rather than silently allowing everything; a
stale-but-valid file keeps serving as last-known-good. On the Python side, the
devpi entrypoint seeds a freshly created `root/constrained` index with the `*`
constraint (block everything) and `min_upstream_age=P0D` so a wiped cache volume
is closed until policy-sync's next successful sync replaces it with the real
policy. With an active age gate, the devpi policy plugin rejects public versions
or direct file URLs whose upload time cannot be verified from PyPI metadata.

**Governance:** policy changes go through PRs and are enforceably auditable:
`registry-policy`'s default branch carries branch protection (no direct pushes except
the admin allowlist, ≥1 required approval), and developers are members of a
`developers` team (packages write, code write for PR branches) — never org Owners.

## Upstream isolation (R7) — the no-fork rule

1. **Gitea runs the stock upstream Docker image.** All customization is runtime
   overlay: `gitea/app.ini.template` (rendered mounted config) and
   `gitea/custom/` templates (Gitea's supported
   `custom/` directory: template/asset overrides). No source patches in v1.
2. **Verdaccio and devpi are consumed as released artifacts.** Our code is plugins
   against their stable plugin APIs (`verdaccio/plugins/*` as npm packages;
   `devpi/artea_devpi_policy` as a Python package). Artea's devpi plugin is
   derived from the small devpi-constrained plugin, but devpi itself is not
   vendored or patched.
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

Config-first, in rendered `gitea/app.ini`: disable registration, disable repo units
(issues/PRs/wiki/projects/actions/releases) by default, landing page → packages
UI, disable migrations/mirrors, disable RSS/federation surface. Template overlay in
rendered `gitea/custom/templates/` de-gits the navbar and home page. What cannot be hidden
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
  supports `policy_url` and `upstream_policy_url` as first-class alternatives to
  files — it polls policy-sync's `GET /policy/npm-rules.yaml` and
  `GET /policy/upstream-policy.yaml` every 10s with ETags, keeps last-known-good
  in memory, and fails closed after a grace window of persistent failure.
  policy-sync serves those endpoints (cluster-internal). Compose keeps file mode
  for npm/upstream policy and also writes `pypi-constraints.txt` for
  debugging. PyPI enforcement state lives in devpi index config (`constraints`
  and `min_upstream_age`) after each successful sync.
- **Bootstrap as a Helm hook Job**: same `scripts/bootstrap.sh` logic and idempotency
  contract, with a token-sink abstraction — `env-file` mode (compose) vs `k8s-secret`
  mode (patches the policy-sync Secret via the API under a namespace-scoped Role and
  triggers a rollout). Runs post-install and post-upgrade.
- **State**: gitea-data PVC is the only store of record; devpi/verdaccio cache PVCs
  are safe to delete (the fail-closed seed makes cache loss benign). Default
  `replicas: 1` everywhere except the stateless gateway.
- **Images**: `ghcr.io/yisding/artea-{devpi,policy-sync,bootstrap,verdaccio-assets}`.
  Production release values digest-pin them; CI builds/pushes both tags and
  digests. Local dev values may use locally-built `:local` tags with
  `pullPolicy: Never` because colima's docker-runtime k3s sees those images
  directly.
- **Local dev contract**: `colima start --kubernetes` (k3s), then
  `kubectl port-forward svc/<gateway> 8080:80` — the e2e suite only knows BASE_URL,
  so S1–S17 run unchanged against compose or K8s.
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
  one configured org (`ARTEA_NAMESPACE`, default `artea`).

## Requirements traceability

| Req | Mechanism | E2E scenario |
|-----|-----------|--------------|
| R1 | Gitea OIDC (Okta) + plugins/auth_request validate everything against Gitea | S11, S12 (+ docs) |
| R2 | npm gateway scope routing → Gitea (scope match, never a fallback); pypi gateway 404-fallback (200 = never consult public) | S2–S4, S6–S9, S17 |
| R3 | Verdaccio filter plugin + tarball middleware + Artea devpi policy plugin, fail-closed | S5, S10, S13, S15 |
| R4 | Stock protocols: npm/pnpm/yarn vs Verdaccio+Gitea; pip/uv/twine vs gateway+Gitea | S2–S10 |
| R5 | Gitea PATs (non-expiring today) | S11 |
| R6 | One PAT publishes (Gitea) and pulls (everywhere) | S2/S3, S6/S7, S11 |
| R7 | Stock images + runtime overlays + plugins + patch-queue escape hatch | audit |

## E2E scenarios (the definition of done for v1)

S1 bootstrap: stack up; admin, configured namespace org, PAT, policy repo seeded, webhook wired.
S2 `npm publish` `@${ARTEA_NAMESPACE}/hello-${ARTEA_NAMESPACE}` with PAT → 201 in Gitea.
S3 `npm install @${ARTEA_NAMESPACE}/hello-${ARTEA_NAMESPACE}` resolves from Gitea via gateway scope routing.
S4 `npm install left-pad` resolves via Verdaccio pull-through from npmjs.
S5 block a `left-pad` version in `npm-rules.yaml`, push, verify it disappears from `npm view left-pad versions`.
S6 `twine upload` a locally built `${ARTEA_NAMESPACE}-hello` wheel → Gitea (artifact stored in Gitea).
S7 `pip install ${ARTEA_NAMESPACE}-hello` via the gateway index.
S8 `pip install six` via gateway → devpi → PyPI pull-through.
S9 precedence: privately publish a name that also exists on PyPI; `pip index versions`
   through the gateway must show ONLY the private versions (proves shadowing).
S10 add `urllib3<2` to constraints, push, verify pip resolves only <2 through the gateway.
S11 same-token: all scenarios above run with one PAT; a `read:package` PAT
    with the required identity scopes can install but is rejected (Gitea answers
    401, not 403) on publish.
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
S16 normalization: non-canonical spellings of a private name (`Acme-Hello`,
    `acme_hello` when `ARTEA_NAMESPACE=acme`) through the gateway still resolve to the private package, never
    falling through to the public mirror.
S17 npm routing compatibility: the legacy two-registry scoped `.npmrc` still
    installs `@${ARTEA_NAMESPACE}/*` unchanged (its publish path — a direct PUT to
    `/api/packages/${ARTEA_NAMESPACE}/npm/`, bypassing the scope match — is the unchanged
    `location /` route), and `%40`/`%2f`-encoded private-scope paths under `/npm/`
    route to Gitea (gateway scope routing), never to Verdaccio.
