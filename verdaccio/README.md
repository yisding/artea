# verdaccio/ — npm pull-through cache

Runs the **stock `verdaccio/verdaccio:6` image** (exact tag pinned in `.env`, per the
no-fork rule R7). Everything of ours is runtime configuration: one rendered config
file and two plugins loaded through Verdaccio's stable plugin API.

| Piece | Purpose |
|-------|---------|
| `config.yaml.template` | Verdaccio config template: `/npm/` url_prefix, npmjs uplink, package rules, plugin wiring |
| `plugins/verdaccio-auth-gitea/` | auth plugin — validates `user:PAT` against Gitea, maps orgs to groups (paginated), 60s positive cache |
| `plugins/verdaccio-filter-artea/` | metadata filter + tarball middleware — enforces `/policy/npm-rules.yaml` (blocked names/scopes/semver ranges, mtime hot-reload, fail-closed) |
| `smoke/` | dev-only: boots verdaccio 6 in-process with `config.yaml.template` + built plugins and asserts the auth/deny contract; **not mounted** into the container |

Design (see `docs/ARCHITECTURE.md`): Verdaccio is **read-only** — publish is denied
for everyone in the generated config; publishes go to Gitea only. The configured
private scope (`@${ARTEA_NAMESPACE}/*`, default `@artea/*`) is fully denied here
(no access, no publish, no proxy) as defense in depth, and there is no anonymous
access; the web UI is disabled.

## Building the plugins (required before first `docker compose up`)

The container does not build anything; plugins are compiled on the host and mounted.

```sh
cd verdaccio/plugins
pnpm install
pnpm build          # tsc -> dist/ in each plugin (CommonJS for verdaccio 6)
pnpm test           # vitest unit tests
```

This is what `make bootstrap` should run for the verdaccio component (note: `dist/`
is gitignored, so plugins must be built before the first `docker compose up`). Both
plugins' runtime dependencies (`semver`, `js-yaml`) are pure JS — no native modules —
so a macOS-built `node_modules` works unchanged inside the Linux container.

To verify config + plugin loading against a real verdaccio without docker:

```sh
cd verdaccio/smoke
pnpm install
pnpm test           # boots verdaccio 6 in-process (~0.5s) and exits
```

## Mounts (compose contract)

| Host path | Container path | Mode | Notes |
|-----------|----------------|------|-------|
| `./.generated/verdaccio/config.yaml` | `/verdaccio/conf/config.yaml` | `ro` | rendered image config |
| `./verdaccio/plugins` | `/verdaccio/plugins` | `ro` | **mount the whole directory** — see below |
| named volume (e.g. `verdaccio-storage`) | `/verdaccio/storage` | rw | package cache; disposable |
| named volume `policy-data` (fixed contract) | `/policy` | `ro` for verdaccio | written by policy-sync, read by the filter plugin |

Mount the **entire** `verdaccio/plugins` directory, never a single plugin folder:
pnpm installs dependencies as *relative* symlinks into `plugins/node_modules/.pnpm`,
so the tree is only self-contained as a whole. The generated config sets
`plugins: /verdaccio/plugins`, and Verdaccio's loader resolves
`/verdaccio/plugins/verdaccio-<name>` for each configured plugin
(`auth: auth-gitea`, `filters: filter-artea`, `middlewares: filter-artea` — the
filter package serves both roles) via each package's `main` (`dist/index.js`).

The image runs as uid 10001 (`$VERDACCIO_USER_UID`). Named volumes initialized by the
image get the right ownership automatically; the read-only bind mounts just need to be
world-readable (default). If you bind-mount storage instead of using a named volume,
`chown -R 10001` it first.

## Network expectations

- Reaches Gitea at `http://gitea:3000` (compose service name; configurable via the
  plugin's `gitea_url` or a `GITEA_URL` env fallback).
- Reaches `https://registry.npmjs.org/` outbound (uplink).
- The gateway proxies `http://localhost:8080/npm/` to `verdaccio:4873`, **stripping
  the `/npm/` prefix** and forwarding the `Host` header; `url_prefix: /npm/` makes
  Verdaccio generate tarball URLs that route back through the gateway.

## Auth model recap

Every request must carry HTTP Basic `user:PAT` (the documented `.npmrc` uses
`_auth=<base64 user:PAT>` + `always-auth=true`). The auth plugin validates against
Gitea per request with a 60s positive cache, so PAT revocation takes effect within a
minute (e2e S12). There is no local user database and `npm adduser` fails by design.

## Policy enforcement recap

policy-sync writes `npm-rules.yaml` from the `${ARTEA_NAMESPACE}/registry-policy` repo into the
shared `/policy` volume; the filter plugin hot-reloads it on mtime change — no
container restart. The same package is also wired as a middleware that rejects
direct tarball downloads of blocked versions with 403 (S13). A missing or
unparsable policy file **fails closed**: packuments are served with no versions and
tarballs get 503 until the file is restored (S15). Schema, fail-closed semantics and
the `fail_open` escape hatch are documented in
`plugins/verdaccio-filter-artea/README.md`.

## Upgrading verdaccio

Bump the pinned tag in `.env`, `make up`, `make e2e`. Plugins target the stable
plugin API (`@verdaccio/types` v10) and need no rebuild for image patch bumps; rebuild
them (`pnpm build`) whenever their own source changes.
