# gateway — nginx single public entrypoint

Stock `nginx` image (tag pinned in `deploy/helm/artea/values.yaml`
`gateway.image`), config plus one small njs file — no source, no custom image.
The njs module (`ngx_http_js_module.so`) ships in the stock image under
`/etc/nginx/modules` and is loaded with `load_module`; it exists only for
PEP 503 name normalization (`njs/pep503.js`).

The nginx.conf is single-sourced as a Helm template
(`deploy/helm/artea/files/gateway/nginx.conf`); Helm renders it into the gateway
ConfigMap. The routing test renders the same template out of the chart with
`helm template ... | yq` (needs `helm` + `yq` on PATH).

nginx resolves the upstream Service names once at startup via its `upstream{}`
blocks (K8s Service ClusterIPs are stable for a Service's lifetime); the
gateway's readiness probe is `/-/artea-gateway/health`.

## Routing table

| Path | Upstream | Auth mechanism |
|------|----------|----------------|
| `/npm/@${ARTEA_NAMESPACE}/**` and `/npm/-/package/@${ARTEA_NAMESPACE}/**` (dist-tag API; scope literal or `%40`-encoded, separator `/` or `%2f`, any letter case) | `gitea:3000` → `/api/packages/${ARTEA_NAMESPACE}/npm/...`. The regex location matches literal and encoded `@` / scope-separator spellings case-insensitively; npm publish/packument paths use `%2f`, which nginx keeps encoded during location matching. The forwarded path comes from a `map` over the **raw** `$request_uri`, so `%2f`/`%40` reach Gitea byte-for-byte. A scoped-location match without a raw-map match, including double-encoded separators (`%252f`/`%2540`), returns `400`, reaching neither upstream. All methods (publish `PUT` included) route identically | gateway `auth_request` → njs guard → Gitea user lookup, `${ARTEA_NAMESPACE}` org-membership check, and package-scope probe, then Gitea enforces package-specific read/write permissions |
| `/npm/**` (everything outside the configured private scope) | `verdaccio:4873` (prefix stripped; Verdaccio `url_prefix=/npm/`) | gateway `auth_request` → same njs guard, so Verdaccio service endpoints (`/-/ping`, `/-/v1/search`, audit) are not anonymous and only org members with package-scoped PATs reach the cache; the `Authorization` header still passes through so Verdaccio's auth plugin authorizes npm-level access against Gitea |
| `/pypi/simple/{name}[/]` | name PEP 503-normalized + trailing slash appended **in the gateway** (see below), then `gitea:3000` → `/api/packages/${ARTEA_NAMESPACE}/pypi/simple/{norm}/`; **on Gitea 404 only** → `devpi:3141` → `/root/constrained/+simple/{norm}/`, where the Artea devpi policy plugin applies PyPI constraints and the shared upstream age gate | same njs guard; org/package-scope successes are cached 30s keyed on the `Authorization` header |
| `/pypi/simple/` (bare full-index page) | `devpi:3141` → `/root/constrained/+simple/` directly (Gitea has no list-all route) | gateway `auth_request` (same guard) |
| `/root/pypi/+(f\|e)/**` (devpi file/external-link URLs; devpi may emit `%2Bf`) | `devpi:3141` (path unchanged) | same njs guard, then an internal Artea devpi policy endpoint confirms the mirror file is allowed by the current constrained-index policy; stale blocked file URLs return 403 |
| `/root/**` (everything else, including raw devpi simple pages) | none | hidden with 404 so clients cannot bypass constraints or the Gitea-first lookup |
| `/-/artea-gateway/health` | none (gateway-local liveness, for the k8s liveness/readiness probes) | none |
| `/api/packages/${ARTEA_NAMESPACE}/npm/**`, `/api/packages/${ARTEA_NAMESPACE}/pypi/**` | `gitea:3000` (raw URI, byte-for-byte) | same gateway guard first, then Gitea enforces package-specific read/write permissions |
| `/api/packages/**` for any other owner or format | none | hidden with 404; v1 exposes only the configured single org and npm/PyPI |
| `/**` (everything else: UI, `/api/v1/...`) | `gitea:3000` (raw URI, byte-for-byte) | Gitea enforces its own auth; no gateway guard |

Auth failures on guarded paths return `401` with `WWW-Authenticate: Basic
realm="Artea"` so pip retries with netrc/keyring credentials. Valid credentials
that fail authorization (non-member of the configured namespace or missing package scope) return
`403` without a Basic challenge. On `/npm/` only nginx-generated auth_request
failures get this mapping: upstream 401/403s from Verdaccio itself (e.g. the policy
middleware's tarball 403, S13) pass through unmodified because
`proxy_intercept_errors` stays off there. npm clients send credentials on every
request (the URL-scoped `_auth` nerf-dart in the documented client config), so
package flows are unaffected by the guard.

## PEP 503 name normalization (njs)

For `/pypi/simple/{name}[/]` the gateway normalizes `{name}` before the
Gitea-first lookup — exactly:

```
norm(name) = name.toLowerCase().replace(/[-_.]+/g, '-')
```

(lowercase, every run of `-`, `_`, `.` collapsed to a single `-`; implemented
in `njs/pep503.js`, wired via `js_set $pypi_project`). Both the Gitea lookup
and the devpi fallback use the **same** normalized name, so non-canonical
spellings (`Acme-Hello`, `acme_hello` when `ARTEA_NAMESPACE=acme`) resolve to the private package and
can never fall through to the public mirror under a spelling Gitea doesn't
recognize (S16). The client-visible URL is not rewritten — normalization is
internal, and both upstreams emit absolute file URLs, so the response body is
identical either way. Missing trailing slashes are appended internally the
same way (`/?$` in the location pattern): the canonicalized request always
re-enters the precedence check, never reaching devpi in a form devpi would
answer with its own redirect.

## The precedence guarantee (R2)

For `GET /pypi/simple/{name}/` the gateway always asks Gitea's org index first
(`proxy_intercept_errors on`). The fallback to the public mirror fires **only**
via `error_page 404 = @public_pypi`:

- Gitea **200** → response served as-is. devpi/PyPI is *never contacted* for that
  request — a privately published name fully shadows the public one
  (dependency-confusion protection, e2e scenario S9).
- Gitea **404** (name not privately published) → proxied to devpi's
  `root/constrained` index with the same normalized name. The Artea devpi policy
  plugin applies `pypi-constraints.txt` and the shared `upstream-policy.yaml`
  age gate.
- Any other Gitea status (401/403/5xx) does **not** fall through; 401/403
  short-circuit into the gateway's Basic challenge (the public mirror is never
  consulted for an unauthenticated/unauthorized request), 5xx pass to the
  client.
- Public devpi-shaped `/root/...` URLs also go to devpi. With a PyPI age gate
  active, the devpi policy plugin rejects unknown direct public file URLs unless
  devpi can associate them with a project and verify the upload time.

## PEP 700 upload-time enrichment (JSON Simple API only)

Neither stock upstream emits PEP 700 `upload-time` in its JSON Simple API
(devpi tops out at `api-version` `1.0`; Gitea ignores the JSON `Accept` header
and serves only PEP 503 HTML), so clients that filter by upload time
(`pip --uploaded-prior-to`, `uv --exclude-newer`, Poetry release-age) fail
against the raw upstreams. The gateway closes this gap **only** for the JSON
path, leaving every other request byte-for-byte unchanged:

- A `map $http_accept $pypi_wants_json` matches
  `application/vnd.pypi.simple.(v1|latest)+json` (bare `application/json` is
  deliberately *not* matched). When it is 1, the `/pypi/simple/{name}/` location
  does `rewrite ^ /_pypi_json_enrich last` **before** its normal Gitea rewrite;
  non-JSON traffic skips the `if` entirely and follows the precedence path above.
- `/_pypi_json_enrich` re-asserts `auth_request` then runs `pep700.enrichRoute`
  (njs, `gateway/njs/pep700.js`). The orchestrator probes Gitea first
  (`/_gitea_simple_probe`, body discarded, client credential forwarded): a
  **200** routes to `policy-sync` with `upstream=gitea` (private wins; devpi is
  never consulted), a **404** routes with `upstream=devpi`. njs is required here
  because the precedence decision must re-route a Gitea *200* to a different body
  source, which `error_page` cannot do.
- `/_enrich` proxies to `policy-sync:8920/pypi/simple-enrich`, which fetches the
  base PEP 691 list (devpi's POST-policy constrained index, or Gitea's HTML),
  joins it with `upload-time` and `size` (PyPI JSON for public; for private,
  Gitea's per-version `created_at` plus per-file `size` from its package-files
  API), bumps `meta.api-version` to `1.1`, adds top-level `versions[]`, and
  returns v1.1 JSON. The reply is cached 30s keyed on `upstream|name|Authorization`
  (credential in the key so a private view never leaks across users) in a
  **dedicated `pypi_enrich` cache zone** — never the small
  `artea_auth` zone, so large Simple-API bodies cannot evict auth entries and
  inflate the S12 revocation latency that auth caching is sized for.
- Availability vs. metadata are decoupled. The base index list is what makes a
  package installable; `upload-time`/`size` are optional metadata. A gateway
  **502** is reserved for the cases that actually break installs: a Gitea probe
  5xx (a Gitea outage must not silently fall through to public for a
  possibly-private name) or an unreachable **base index**. If the base list is
  reachable but the upstream upload-time source (pypi.org JSON) is momentarily
  down and nothing is cached, policy-sync serves the base v1.1 list *without*
  upload-time rather than 502 — a plain `pip/uv install <public pkg>` keeps
  working through a pypi.org blip, and a time-filtering client just won't match
  the un-stamped files (the same safe direction as a per-file metadata miss).
  Such a metadata-degraded document is not cached, so the next request retries.
- Composition: the public base list is devpi's `root/constrained` page, already
  filtered by `pypi-constraints.txt` and `upstream-policy.yaml`, so enrichment
  only annotates files the age gate already permits.

## npm scope routing (gateway-enforced)

npm precedence is a **scope match, never a 404-fallback** — a fallback would
reintroduce dependency confusion; the scope match keeps private-scope names structurally
unable to reach Verdaccio or npmjs (an unpublished private name 404s, full
stop). Mechanism, in `deploy/helm/artea/files/gateway/nginx.conf`:

- A regex location (`~* ^/npm/(?:-/package/)?(?:@|%40)__ARTEA_NAMESPACE__(?:%2f|/)`) peels private-scope traffic
  off the `/npm/` prefix route before Verdaccio: packument/publish/tarball/
  unpublish paths plus the dist-tag API under `/-/package/`. It explicitly
  matches both literal and encoded `@` / scope-separator spellings because npm
  sends packument and publish paths as `%40<namespace>%2f...`, and nginx keeps `%2f`
  encoded during location matching. The match is case-insensitive, so
  case-variant spellings of the scope (`@ACME/...` when the namespace is `acme`) also route to Gitea
  (where they 404) instead of falling through to Verdaccio's npmjs uplink
  (the npm analogue of the pypi route's PEP 503 case-folding). Requiring the
  scope separator (`/` or `%2f`) keeps lookalike scopes such as `@acme-evil/*` out.
- The forwarded path is not a rewrite: a `map` over the **raw** `$request_uri`
  produces `/api/packages/${ARTEA_NAMESPACE}/npm/...`, preserving npm's `%2f`-encoded
  scoped names byte-for-byte (a rewrite would operate on the decoded `$uri`
  and corrupt them).
- If a URI enters the scoped location but the raw URI fits neither map pattern,
  the map yields `""` and the location returns `400`: such a request reaches
  neither Verdaccio nor Gitea. This covers percent-encoded scope letters and
  double-encoded separators (`%252f`, `%2540`); no canonical private-scope name can
  leak by these spellings.
- The private-scope route still runs the gateway guard before proxying to Gitea,
  so non-org users and PATs without package scope fail before any Gitea 404 can
  blur "not found" with "not allowed". Gitea remains the final package
  permission check for reads and writes.

Verdaccio's deny rule for `@${ARTEA_NAMESPACE}/*` stays as defense in depth,
and the legacy client-side scope routing (`@${ARTEA_NAMESPACE}:registry=` in
`.npmrc`) keeps working — it
hits `/api/packages/...` directly and never exercises this location (S17).
Client contract, including the npm-publish credential-preflight caveat (one
credential value on two nerf-dart lines): `docs/guides/clients-npm.md` and the
CLIENT CAVEAT comment in `deploy/helm/artea/files/gateway/nginx.conf`.

## Buffering / streaming choices

- `client_max_body_size 512m` — npm/twine uploads of large artifacts.
- `proxy_buffering off` (responses) — tarballs/wheels stream straight through, no
  disk spooling in the gateway; trade-off: a slow client holds its upstream
  connection open. Fine for an internal registry.
- `proxy_request_buffering off` + `proxy_http_version 1.1` (requests) — uploads
  stream to Gitea instead of being spooled (up to 512 MB) to gateway disk first.
- The auth subrequest location re-enables `proxy_buffering` because
  `proxy_cache` requires it.
- `absolute_redirect off` — the container listens on 80 but is published as 8080;
  relative `Location` headers avoid emitting `http://localhost/...` (port lost).
- `X-Forwarded-For/-Proto/-Host`, `X-Real-IP`, `Host $http_host` set for all
  upstreams; `X-outside-url` is set for devpi paths so generated public package
  links stay on the gateway origin.

## Auth-result cache

The njs guard first hits Gitea's `/api/v1/user` forwarding the client's
`Authorization` header, then checks `${ARTEA_NAMESPACE}` org membership for the
returned login, then probes Gitea's package-management API
(`/api/v1/packages/${ARTEA_NAMESPACE}/?type=pypi&limit=1`) to prove the PAT has
package scope. Successful user/org/package-scope results are cached for 30 s
keyed on the raw header value plus the probe type; failures are never cached,
so bad credentials or removed org memberships are re-checked every time. Net
effect: PAT revocation takes effect on guarded paths within 30 s for the
gateway guard itself (budget is 60 s, scenario S12). Trade-offs, accepted and
documented:

- Cached entries (header value + small auth response metadata) live on disk
  inside the container's ephemeral layer at `/var/cache/nginx/artea_auth` —
  never on a volume. Restarting the container clears them. The much larger
  PEP 700 enriched Simple-API bodies use a separate `pypi_enrich` zone
  (`/var/cache/nginx/pypi_enrich`), so they cannot evict these auth entries; the
  30 s revocation budget above is computed against an auth cache that enrichment
  never touches.
- The cache key is the credential itself; nginx stores it md5-hashed in the file
  name but verbatim inside the cache file. Same blast radius as nginx access
  logs; acceptable for v1.

## Known edge cases — probe these in e2e

1. **404-vs-401 interplay (the big one).** The fallback keys on Gitea's 404, and
   Gitea answers 404 both for "name not published" *and* for "exists but this
   authenticated user may not see it" (it hides private resources). The gateway
   first validates that the PAT belongs to a `${ARTEA_NAMESPACE}` org member
   with package scope, so only authorized org members can reach the fallback
   path at all. e2e S9 pins the private-name precedence check, and gateway
   routing tests pin non-member rejection.
2. **PAT scopes for the guard.** The guard first calls `/api/v1/user` so Basic
   `user:PAT` and npm's token-style Gitea auth both resolve to a Gitea login. It
   then calls `/api/v1/orgs/${ARTEA_NAMESPACE}/members/{login}`, which requires
   the `read:organization` token scope, and probes the package-management API so
   a user/org-only token cannot pull packages. Verdaccio's user check also
   requires `read:user`. **Bootstrap must mint PATs with `read:user` and
   `read:organization` in addition to `read:package`/`write:package`.** Basic
   auth with the account password is not the supported client path for package
   installs.
3. **devpi link generation.** devpi's `+simple/` pages are served to the client
   under `/pypi/simple/{name}/` but generated for `/root/constrained/+simple/{name}/`
   — one path segment deeper. If devpi emits *relative* file hrefs
   (`../../+f/...`), pip resolves them to `/pypi/+f/...`, which routes to Gitea
   and 404s. The gateway already sends `X-outside-url`; **devpi must be run with
   `--absolute-urls`** so file links come out as
   `http://localhost:8080/root/pypi/%2Bf/...`, which the restricted devpi file
   location serves after canonicalizing `%2Bf` to `+f` for the constrained-link
   check. S8 is the canary. Raw `/root/*/+simple/` routes intentionally stay
   hidden.
4. **PEP 503 name normalization happens in the gateway.** The Gitea-first
   lookup and the devpi fallback both use the njs-normalized name (see "PEP 503
   name normalization" above) — shadowing no longer relies on the client, Gitea,
   or devpi normalizing. Non-canonical spellings (`Acme-Hello`, `acme_hello`)
   of a private name hit the private package and never fall through to the
   public mirror (S16; probe S9 with such a name too). devpi only ever sees
   canonical names, so it has no reason to redirect; if it ever does, the
   fallback location's `proxy_redirect` maps the `Location` back into
   `/pypi/simple/...` so the precedence check re-runs.
5. **Trailing slashes are canonicalized in the gateway.** The pypi location
   matches `/pypi/simple/{name}` with or without the trailing slash and always
   forwards the slashed form to Gitea (whose router trims it anyway) and to
   devpi (`+simple/{norm}/`, devpi's canonical form) — previously the slashless
   form reached devpi as-is and devpi answered with a 302 to its own path,
   letting redirect-following clients skip the Gitea-first precedence check.
   The bare `/pypi/simple` still redirects to `/pypi/simple/`.
6. **The full-index page.** `GET /pypi/simple/` (empty project name) is routed
   straight to devpi's mirror index (still behind the auth guard) — Gitea has
   no list-all route, so asking it first would be a guaranteed 404. That page
   is the complete pypi.org name list, several MB. pip doesn't fetch it during
   `install`; nothing breaks, just don't be surprised in logs.
7. **Scoped-npm URL encoding.** Gitea-bound paths carry the raw URI
   byte-for-byte (`%2f` preserved — covered by a test): direct
   `/api/packages/...` paths via exact locations, and private-scope traffic
   under `/npm/` via the `$private_npm_uri` map over the raw `$request_uri` (see
   "npm scope routing" above). For other scopes the `/npm/` location rewrites
   (strips the prefix), which makes nginx re-encode the URI, so Verdaccio may
   see `@scope/name` where the client sent `@scope%2fname`. Verdaccio's own
   documented nginx sub-path setup has the same property and it accepts both
   forms; S4/S5 exercise this.
8. **Auth caching vs revocation.** A revoked PAT keeps working on guarded paths
   while positive cache entries are live. The gateway and Verdaccio positive
   auth caches are each 30 s, so the conservative worst-case remains within
   S12's 60 s budget.
9. **`/npm/` is guarded but transparent.** Anonymous requests to Verdaccio's
   service endpoints (`/-/ping`, `/-/v1/search`, audit) get the 401 Basic
   challenge instead of answers ("anonymous access: none, anywhere").
   Authenticated requests pass the `Authorization` header through to Verdaccio
   untouched, and Verdaccio's own 401/403 responses (policy tarball 403, S13)
   reach the client unmodified — `proxy_intercept_errors` stays off on
   `/npm/`, so only nginx-generated auth_request failures are re-mapped to the
   Basic challenge. Configured private-scope paths peel off to Gitea before
   Verdaccio but still run the same gateway guard first. S2–S5 must keep
   passing with the guard in place.

## Validating the config

Without docker, with a local nginx binary **that has the njs module** (the
config `load_module`s it; nginx.org Linux packages provide
`nginx-module-njs`, homebrew's nginx does not):

```sh
# functional routing test (boots nginx + stub upstreams on loopback, ~1 s).
# Renders the single-source config via helm (needs helm + yq on PATH), then
# substitutes container paths/ports and the njs module path automatically;
# skips with a pointer here when the host nginx lacks njs.
python3 gateway/test/test_routing.py
```

To render the config straight out of the chart (the same source the gateway
ConfigMap ships) for inspection or `nginx -t`:

```sh
# extract the rendered nginx.conf from the gateway ConfigMap
helm template artea deploy/helm/artea --show-only templates/gateway.yaml \
  | yq '. | select(.kind == "ConfigMap")'
```
