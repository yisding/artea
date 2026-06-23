# verdaccio/ — npm pull-through cache

Runs the **stock `verdaccio/verdaccio:6` image** (exact tag pinned in
`deploy/helm/artea/values.yaml` `verdaccio.image`, per the
no-fork rule R7). Everything of ours is runtime configuration: one rendered config
file and two plugins loaded through Verdaccio's stable plugin API.

The config is single-sourced as a Helm template at
`deploy/helm/artea/files/verdaccio/config.yaml` — Helm renders it and Kubernetes
delivers it via the `artea-verdaccio-config` ConfigMap (no compose variant). The
verdaccio smoke test renders it out of the chart for local validation.

| Piece | Purpose |
|-------|---------|
| `deploy/helm/artea/files/verdaccio/config.yaml` | Verdaccio config (single source): `/npm/` url_prefix, npmjs uplink, package rules, plugin wiring |
| `plugins/verdaccio-auth-gitea/` | auth plugin — validates `user:PAT` against Gitea, rejects users outside the configured namespace org, maps that org/team membership to groups (paginated), 30s positive cache |
| `plugins/verdaccio-filter-artea/` | metadata filter + tarball middleware — enforces `/policy/npm-rules.yaml` (blocked names/scopes/semver ranges) plus `/policy/upstream-policy.yaml` (minimum upstream age), mtime hot-reload, fail-closed |
| `smoke/` | dev-only: boots verdaccio 6 in-process with the chart-rendered config (policy delivered via a local `policy_file` so no policy-sync is needed) + built plugins and asserts the auth/deny contract; **not mounted** into the container |

Design (see `docs/ARCHITECTURE.md`): Verdaccio is **read-only** — publish is denied
for everyone in the generated config; publishes go to Gitea only. The configured
private scope (`@${ARTEA_NAMESPACE}/*`, default `@artea/*`) is fully denied here
(no access, no publish, no proxy) as defense in depth, and there is no anonymous
access; the web UI is disabled.

## Building the plugins (required before building the verdaccio-assets image)

The container does not build anything; plugins are compiled on the host and mounted.

```sh
cd verdaccio/plugins
pnpm install --frozen-lockfile
pnpm build          # tsc -> dist/ in each plugin (CommonJS for verdaccio 6)
pnpm test           # vitest unit tests
```

`make plugins` runs `pnpm install --frozen-lockfile && pnpm build` for the
verdaccio component (it does **not** run `pnpm test`) (note: `dist/` is gitignored,
so plugins must be built before the verdaccio-assets image (`make images`) or
`make dev`). Both
plugins' runtime dependencies (`semver`, `js-yaml`) are pure JS — no native modules —
so a macOS-built `node_modules` works unchanged inside the Linux container.

To verify config + plugin loading against a real verdaccio without docker:

```sh
cd verdaccio/smoke
pnpm install
pnpm test           # boots verdaccio 6 in-process (~0.5s) and exits
```

## Config and plugin delivery (k8s)

- The config arrives via the `artea-verdaccio-config` ConfigMap, mounted at
  `/verdaccio/conf/config.yaml`.
- The package cache is a disposable PVC at `/verdaccio/storage`.
- The plugins are delivered by an init container (`copy-plugins`) that copies the
  **built** plugin tree out of the verdaccio-assets image into an emptyDir, mounted
  read-only at `/verdaccio/plugins`.

The init container copies the **entire** plugins tree, never a single plugin folder:
pnpm installs dependencies as *relative* symlinks into `plugins/node_modules/.pnpm`,
so the tree is only self-contained as a whole. The config sets
`plugins: /verdaccio/plugins`, and Verdaccio's loader resolves
`/verdaccio/plugins/verdaccio-<name>` for each configured plugin
(`auth: auth-gitea`, `filters: filter-artea`, `middlewares: filter-artea` — the
filter package serves both roles) via each package's `main` (`dist/index.js`).

The image runs as uid 10001 (`$VERDACCIO_USER_UID`); the PVC is initialized with
that ownership and the read-only plugin/config mounts just need to be
world-readable (default). Policy is **not** delivered via a shared `/policy`
volume — it comes over HTTP (`policy_url`, see below).

## Network expectations

- Reaches Gitea at `http://artea-gitea-http:3000` (cluster Service DNS; configurable via the
  plugin's `gitea_url` or a `GITEA_URL` env fallback).
- Reaches `https://registry.npmjs.org/` outbound (uplink).
- The gateway proxies `http://localhost:8080/npm/` to `artea-verdaccio:4873`, **stripping
  the `/npm/` prefix** and forwarding the `Host` header; `url_prefix: /npm/` makes
  Verdaccio generate tarball URLs that route back through the gateway.

## Auth model recap

Every request must carry HTTP Basic `user:PAT` (the documented `.npmrc` uses
`_auth=<base64 user:PAT>` + `always-auth=true`). The auth plugin validates against
Gitea per request, rejects valid Gitea users outside the configured namespace
org (`ARTEA_NAMESPACE`, default `artea`), and
uses a 30s positive cache. The gateway has its own 30s positive auth cache, so
the conservative npm pull-through revocation guarantee is still within 60s (e2e
S12). There is no local user database and `npm adduser` fails by design.

## Policy enforcement recap

policy-sync serves `npm-rules.yaml` and `upstream-policy.yaml` (from the
`${ARTEA_NAMESPACE}/registry-policy` repo) over HTTP; the filter plugin polls
them (`policy_url`/`upstream_policy_url`) with ETag, keeps the last-known-good
in memory, and fails closed after a grace window — no container restart. The same
package is also wired as a middleware that rejects direct tarball downloads of
blocked or too-new versions with 403 (S13). A missing or unparsable policy
**fails closed**: packuments are served with no versions and tarballs get 503
until policy-sync is reachable again (S15). Schema and fail-closed semantics are documented
in `plugins/verdaccio-filter-artea/README.md`.

## Upgrading verdaccio

Bump the pinned tag in `deploy/helm/artea/values.yaml` (`verdaccio.image.tag`),
`make dev`, `make e2e`. Plugins target the stable
plugin API (`@verdaccio/types` v10) and need no rebuild for image patch bumps; rebuild
them (`pnpm build`) whenever their own source changes.
