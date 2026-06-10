# gateway — nginx single public entrypoint

Stock `nginx` image (pin the exact tag in `.env`, e.g. `NGINX_VERSION=1.29.4`),
config only — no source, no custom image. Compose wiring:

```yaml
gateway:
  image: nginx:${NGINX_VERSION}
  container_name: gateway
  ports:
    - "8080:80"
  volumes:
    - ./gateway/nginx.conf:/etc/nginx/nginx.conf:ro
```

The config relies on Docker's embedded DNS (`resolver 127.0.0.11`) and resolves
upstream names per-request, so the gateway starts and stays up regardless of the
other containers' state (no `depends_on` ordering required for nginx itself).

## Routing table

| Path | Upstream | Auth mechanism |
|------|----------|----------------|
| `/npm/**` | `verdaccio:4873` (prefix stripped; Verdaccio `url_prefix=/npm/`) | none at gateway; Verdaccio's auth plugin validates the passed-through `Authorization` header against Gitea |
| `/pypi/simple/{name}/` | `gitea:3000` → `/api/packages/artea/pypi/simple/{name}/`; **on Gitea 404 only** → `devpi:3141` → `/root/constrained/+simple/{name}/` | gateway `auth_request` → Gitea `GET /api/v1/user` (Basic `user:PAT`); 200s cached 30s keyed on the `Authorization` header |
| `/root/**` (devpi simple pages, file downloads) | `devpi:3141` (path unchanged) | gateway `auth_request` (same guard; devpi itself has no auth) |
| `/-/artea-gateway/health` | none (gateway-local liveness, for compose healthchecks) | none |
| `/**` (everything else: UI, `/api/v1/...`, `/api/packages/artea/npm/...`, `/api/packages/artea/pypi/...` uploads + files) | `gitea:3000` (raw URI, byte-for-byte) | Gitea enforces its own auth; no gateway guard |

Auth failures on guarded paths return `401` with `WWW-Authenticate: Basic
realm="Artea"` so pip retries with netrc/keyring credentials. A `403` from the
auth subrequest (e.g. a PAT lacking the required API scope) is also mapped to
this `401` challenge.

## The precedence guarantee (R2)

For `GET /pypi/simple/{name}/` the gateway always asks Gitea's org index first
(`proxy_intercept_errors on`). The fallback to the public mirror fires **only**
via `error_page 404 = @public_pypi`:

- Gitea **200** → response served as-is. devpi/PyPI is *never contacted* for that
  request — a privately published name fully shadows the public one
  (dependency-confusion protection, e2e scenario S9).
- Gitea **404** (name not privately published) → proxied to devpi's
  `root/constrained` index, which mirrors pypi.org filtered by
  `pypi-constraints.txt` (R3).
- Any other Gitea status (401/403/5xx) does **not** fall through; 401/403 become
  the gateway's Basic challenge, 5xx pass to the client.

npm needs no such logic at the gateway: the npm client itself routes `@artea/*`
to Gitea via scope config, everything else to `/npm/` (Verdaccio).

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
every time. Net effect: PAT revocation takes effect on devpi-bound paths within
30 s (budget is 60 s, scenario S12). Trade-offs, accepted and documented:

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
   gateway converts it to a 401 challenge, but pip still can't get in).
   **Bootstrap must mint PATs with `read:user` in addition to
   `read:packages`/`write:packages`.** Basic auth with the account password (or
   a token used as Basic password, which Gitea treats as full-scope) is not
   affected.
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
4. **PEP 503 name normalization.** pip sends lowercase normalized names. Gitea
   normalizes `.`/`_` → `-` server-side and matches case-insensitively; devpi
   normalizes too. So `twine upload` of `Artea_Hello` is still found at
   `/pypi/simple/artea-hello/` and shadowing holds. Probe S9 with such a name.
   Manually-crafted URLs with uppercase/underscores may get devpi redirects —
   harmless, but check the `Location` stays on `http://localhost:8080`.
5. **Trailing slashes.** pip requests `/pypi/simple/{name}/` (trailing slash);
   Gitea's router trims trailing slashes so both forms work, and the rewrite
   passes the slash through to devpi (`+simple/{name}/`), which is devpi's
   canonical form. The bare `/pypi/simple` redirects to `/pypi/simple/`.
6. **The full-index page.** `GET /pypi/simple/` (empty project name) 404s in
   Gitea (it has no list-all route) and falls through to devpi's mirror index —
   the complete pypi.org name list, several MB. pip doesn't fetch it during
   `install`; nothing breaks, just don't be surprised in logs.
7. **Scoped-npm URL encoding.** Raw URIs go to Gitea byte-for-byte (`%2f`
   preserved — covered by a test). The `/npm/` location rewrites (strips the
   prefix), which makes nginx re-encode the URI, so Verdaccio may see
   `@scope/name` where the client sent `@scope%2fname`. Verdaccio's own
   documented nginx sub-path setup has the same property and it accepts both
   forms; S4/S5 exercise this.
8. **Auth caching vs revocation.** A revoked PAT keeps working on
   devpi-bound paths for up to 30 s (S12's budget is 60 s, and Verdaccio's own
   plugin cache is 60 s). Gitea-bound paths (npm publish/install of `@artea/*`,
   twine) are revoked instantly — no gateway cache involved.

## Validating the config

Without docker, with any local nginx binary (config logic is host-agnostic;
only container paths/ports need substituting):

```sh
# pure syntax check
T=$(mktemp -d) && mkdir -p "$T/logs" "$T/cache" && \
sed -e "s|/var/log/nginx|$T/logs|g" -e "s|/var/cache/nginx|$T/cache|g" \
    -e "s|/var/run/nginx.pid|$T/nginx.pid|g" gateway/nginx.conf > "$T/nginx.conf" && \
nginx -t -p "$T" -c "$T/nginx.conf"

# functional routing test (boots nginx + stub upstreams on loopback, ~1 s)
python3 gateway/test/test_routing.py
```

With docker (what the integrator should run):

```sh
docker run --rm -v "$PWD/gateway/nginx.conf:/etc/nginx/nginx.conf:ro" nginx:${NGINX_VERSION} nginx -t
```
