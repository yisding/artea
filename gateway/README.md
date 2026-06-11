# gateway — nginx single public entrypoint

Stock `nginx` image (pin the exact tag in `.env`, e.g. `NGINX_VERSION=1.29.4`),
config plus one small njs file — no source, no custom image. The njs module
(`ngx_http_js_module.so`) ships in the stock image under `/etc/nginx/modules`
and is loaded with `load_module`; it exists only for PEP 503 name
normalization (`njs/pep503.js`). Compose wiring:

```yaml
gateway:
  image: nginx:${NGINX_VERSION}
  container_name: gateway
  ports:
    - "8080:80"
  volumes:
    - ./gateway/nginx.conf:/etc/nginx/nginx.conf:ro
    - ./gateway/njs:/etc/nginx/njs:ro
```

The config relies on Docker's embedded DNS (`resolver 127.0.0.11`) and resolves
upstream names per-request, so the gateway starts and stays up regardless of the
other containers' state (no `depends_on` ordering required for nginx itself).

## Routing table

| Path | Upstream | Auth mechanism |
|------|----------|----------------|
| `/npm/@artea/**` and `/npm/-/package/@artea/**` (dist-tag API; scope literal or `%40`-encoded, separator `/` or `%2f`, any letter case) | `gitea:3000` → `/api/packages/artea/npm/...`. The regex location matches the **decoded** `$uri` case-insensitively (one pattern covers all encodings and case spellings — a case variant routes to Gitea and 404s there, never reaching Verdaccio/npmjs) but the forwarded path comes from a `map` over the **raw** `$request_uri`, so `%2f`/`%40` reach Gitea byte-for-byte. Decoded match without a raw match (percent-encoded scope *letters*, e.g. `@%61rtea`) → `400`, reaching neither upstream; double-encoded separators (`%252f`/`%2540`) decode once to literal `%2f`/`%40` text, never match this row, and stay on the Verdaccio row below (where the malformed name is rejected). All methods (publish `PUT` included) route identically | none at the gateway — Gitea enforces its own auth (Basic `user:PAT` or token); no `auth_request` |
| `/npm/**` (everything not `@artea`-scoped) | `verdaccio:4873` (prefix stripped; Verdaccio `url_prefix=/npm/`) | gateway `auth_request` → Gitea `GET /api/v1/user` (same guard + 30s cache as pypi), so Verdaccio service endpoints (`/-/ping`, `/-/v1/search`, audit) are not anonymous; the `Authorization` header still passes through so Verdaccio's auth plugin authorizes npm-level access against Gitea |
| `/pypi/simple/{name}[/]` | name PEP 503-normalized + trailing slash appended **in the gateway** (see below), then `gitea:3000` → `/api/packages/artea/pypi/simple/{norm}/`; **on Gitea 404 only** → `devpi:3141` → `/root/constrained/+simple/{norm}/` (same normalized name) | gateway `auth_request` → Gitea `GET /api/v1/user` (Basic `user:PAT`); 200s cached 30s keyed on the `Authorization` header |
| `/pypi/simple/` (bare full-index page) | `devpi:3141` → `/root/constrained/+simple/` directly (Gitea has no list-all route) | gateway `auth_request` (same guard) |
| `/root/**` (devpi simple pages, file downloads) | `devpi:3141` (path unchanged) | gateway `auth_request` (same guard; devpi itself has no auth) |
| `/-/artea-gateway/health` | none (gateway-local liveness, for compose healthchecks) | none |
| `/**` (everything else: UI, `/api/v1/...`, `/api/packages/artea/npm/...`, `/api/packages/artea/pypi/...` uploads + files) | `gitea:3000` (raw URI, byte-for-byte) | Gitea enforces its own auth; no gateway guard |

Auth failures on guarded paths return `401` with `WWW-Authenticate: Basic
realm="Artea"` so pip retries with netrc/keyring credentials. A `403` from the
auth subrequest (e.g. a PAT lacking the required API scope) is also mapped to
this `401` challenge. On `/npm/` only nginx-generated auth_request failures get
this mapping: upstream 401/403s from Verdaccio itself (e.g. the policy
middleware's tarball 403, S13) pass through unmodified because
`proxy_intercept_errors` stays off there. npm clients send credentials on every
request (`always-auth=true` in the documented client config), so package flows
are unaffected by the guard.

## PEP 503 name normalization (njs)

For `/pypi/simple/{name}[/]` the gateway normalizes `{name}` before the
Gitea-first lookup — exactly:

```
norm(name) = name.toLowerCase().replace(/[-_.]+/g, '-')
```

(lowercase, every run of `-`, `_`, `.` collapsed to a single `-`; implemented
in `njs/pep503.js`, wired via `js_set $pypi_project`). Both the Gitea lookup
and the devpi fallback use the **same** normalized name, so non-canonical
spellings (`Artea-Hello`, `artea_hello`) resolve to the private package and
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
  `root/constrained` index, which mirrors pypi.org filtered by
  `pypi-constraints.txt` (R3), asked with the same normalized name.
- Any other Gitea status (401/403/5xx) does **not** fall through; 401/403
  short-circuit into the gateway's Basic challenge (the public mirror is never
  consulted for an unauthenticated/unauthorized request), 5xx pass to the
  client.
- Should devpi ever answer the fallback with a redirect, a `proxy_redirect`
  regex maps its `Location` back into `/pypi/simple/...` so a
  redirect-following client re-enters this precedence check instead of hitting
  `/root/...` directly. (It shouldn't happen: the gateway only sends devpi
  canonical names with trailing slashes.)

## npm scope routing (gateway-enforced)

npm precedence is a **scope match, never a 404-fallback** — a fallback would
reintroduce dependency confusion; the scope match keeps `@artea` structurally
unable to reach Verdaccio or npmjs (an unpublished private name 404s, full
stop). Mechanism, in `nginx.conf`:

- A regex location (`~* ^/npm/(?:-/package/)?@artea/`) peels `@artea` traffic
  off the `/npm/` prefix route before Verdaccio: packument/publish/tarball/
  unpublish paths plus the dist-tag API under `/-/package/`. It matches the
  **decoded** `$uri` — nginx decodes `%2f`/`%40` before location matching, so
  one pattern covers the encoded and literal spellings — case-insensitively,
  so case-variant spellings of the scope (`@ARTEA/...`) also route to Gitea
  (where they 404) instead of falling through to Verdaccio's npmjs uplink
  (the npm analogue of the pypi route's PEP 503 case-folding). The trailing
  slash (the decoded scope separator) keeps `@artea-evil/*` out.
- The forwarded path is not a rewrite: a `map` over the **raw** `$request_uri`
  produces `/api/packages/artea/npm/...`, preserving npm's `%2f`-encoded
  scoped names byte-for-byte (a rewrite would operate on the decoded `$uri`
  and corrupt them).
- If the decoded URI matched but the raw URI fits neither map pattern — the
  scope *letters* themselves are percent-encoded (e.g. `/npm/@%61rtea/x`,
  which decodes to `@artea/x`) — the map yields `""` and the location returns
  `400`: such a request reaches neither Verdaccio nor Gitea. Double-encoded
  separators (`%252f`, `%2540`) are a different case: they decode once to
  literal `%2f`/`%40` *text*, so the decoded URI never looks `@artea`-scoped,
  this location never matches, and the request falls through to Verdaccio —
  where it misses the `@artea/*` deny pattern (the decoded-once name is
  `%40artea%2f...`, not `@artea/...`) but is rejected as a malformed package
  name; no canonical `@artea/*` name can be expressed that way, so nothing
  private can leak by this spelling.
- No `auth_request` on this route: it is Gitea-bound and Gitea authenticates
  itself. The guard's job on `/npm/` remains the Verdaccio-bound paths.

Verdaccio's deny rule for `@artea/*` stays as defense in depth, and the legacy
client-side scope routing (`@artea:registry=` in `.npmrc`) keeps working — it
hits `/api/packages/...` directly and never exercises this location (S17).
Client contract, including the npm-publish credential-preflight caveat (one
credential value on two nerf-dart lines): `docs/guides/clients-npm.md` and the
CLIENT CAVEAT comment in `nginx.conf`.

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
  upstreams; `X-outside-url` is set for devpi (its documented reverse-proxy
  mechanism; Gitea/Verdaccio ignore it).

## Auth-result cache

`auth_request` subrequests hit Gitea `GET /api/v1/user` forwarding the client's
`Authorization` header. Successful (200) results are cached for 30 s keyed on the
raw header value; failures are never cached, so bad credentials are re-checked
every time. Net effect: PAT revocation takes effect on guarded paths (`/npm/`
and all devpi-bound paths) within 30 s (budget is 60 s, scenario S12).
Trade-offs, accepted and documented:

- Cached entries (header value + small user JSON) live on disk inside the
  container's ephemeral layer at `/var/cache/nginx/artea_auth` — never on a
  volume. Restarting the container clears them.
- The cache key is the credential itself; nginx stores it md5-hashed in the file
  name but verbatim inside the cache file. Same blast radius as nginx access
  logs; acceptable for v1.

## Known edge cases — probe these in e2e

1. **404-vs-401 interplay (the big one).** The fallback keys on Gitea's 404, and
   Gitea answers 404 both for "name not published" *and* for "exists but this
   authenticated user may not see it" (it hides private resources). With the v1
   single public org `artea` every authenticated user can read org packages, so
   404 reliably means "not private". If the org is ever switched to
   private/limited visibility, non-member users would silently fall through to
   the public mirror for *private* names — reopening dependency confusion. e2e
   S9 should run once as an org member and once as a plain authenticated
   non-member to pin this down.
2. **PAT scopes for the guard.** The guard calls `/api/v1/user`, which in Gitea
   requires the `read:user` token scope — `read:packages` alone yields 403 (the
   gateway converts it to a 401 challenge, but pip still can't get in). The
   same now applies to every Verdaccio-bound `/npm/` request (`@artea`-scoped
   paths skip the guard; Gitea authenticates them itself). **Bootstrap must mint PATs with
   `read:user` in addition to `read:packages`/`write:packages`.** Basic auth
   with the account password (or a token used as Basic password, which Gitea
   treats as full-scope) is not affected.
3. **devpi link generation.** devpi's `+simple/` pages are served to the client
   under `/pypi/simple/{name}/` but generated for `/root/constrained/+simple/{name}/`
   — one path segment deeper. If devpi emits *relative* file hrefs
   (`../../+f/...`), pip resolves them to `/pypi/+f/...`, which routes to Gitea
   and 404s. The gateway already sends `X-outside-url`; **devpi must be run with
   `--absolute-urls`** so file links come out as
   `http://localhost:8080/root/constrained/+f/...`, which the `/root/` location
   serves. S8 is the canary. (Fallback mitigations if needed: a
   `/pypi/+...` → `/root/constrained/+...` compatibility location, or switching
   the fallback from proxy to a 302 redirect into `/root/...`.)
4. **PEP 503 name normalization happens in the gateway.** The Gitea-first
   lookup and the devpi fallback both use the njs-normalized name (see "PEP 503
   name normalization" above) — shadowing no longer relies on the client, Gitea,
   or devpi normalizing. Non-canonical spellings (`Artea-Hello`, `artea_hello`)
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
   byte-for-byte (`%2f` preserved — covered by a test): `/api/packages/...`
   via `location /` (no rewrite), and `@artea` traffic under `/npm/` via the
   `$artea_npm_uri` map over the raw `$request_uri` (see "npm scope routing"
   above). For other scopes the `/npm/` location rewrites (strips the prefix),
   which makes nginx re-encode the URI, so Verdaccio may see `@scope/name`
   where the client sent `@scope%2fname`. Verdaccio's own documented nginx
   sub-path setup has the same property and it accepts both forms; S4/S5
   exercise this.
8. **Auth caching vs revocation.** A revoked PAT keeps working on guarded
   paths — `/npm/` and devpi-bound — for up to 30 s (S12's budget is 60 s, and
   Verdaccio's own plugin cache is 60 s). Gitea-bound paths (npm
   publish/install of `@artea/*`, twine) are revoked instantly — no gateway
   cache involved.
9. **`/npm/` is guarded but transparent.** Anonymous requests to Verdaccio's
   service endpoints (`/-/ping`, `/-/v1/search`, audit) get the 401 Basic
   challenge instead of answers ("anonymous access: none, anywhere").
   Authenticated requests pass the `Authorization` header through to Verdaccio
   untouched, and Verdaccio's own 401/403 responses (policy tarball 403, S13)
   reach the client unmodified — `proxy_intercept_errors` stays off on
   `/npm/`, so only nginx-generated auth_request failures are re-mapped to the
   Basic challenge. `@artea`-scoped paths peel off to Gitea before the guard
   and are authenticated by Gitea itself (their 401s come from Gitea, not the
   gateway). S2–S5 must keep passing with the guard in place.

## Validating the config

Without docker, with a local nginx binary **that has the njs module** (the
config `load_module`s it; nginx.org Linux packages provide
`nginx-module-njs`, homebrew's nginx does not):

```sh
# functional routing test (boots nginx + stub upstreams on loopback, ~1 s).
# Substitutes container paths/ports and the njs module path automatically;
# skips with a pointer here when the host nginx lacks njs.
python3 gateway/test/test_routing.py
```

With docker (what the integrator should run; the stock image ships njs):

```sh
docker run --rm \
  -v "$PWD/gateway/nginx.conf:/etc/nginx/nginx.conf:ro" \
  -v "$PWD/gateway/njs:/etc/nginx/njs:ro" \
  nginx:${NGINX_VERSION} nginx -t
```
