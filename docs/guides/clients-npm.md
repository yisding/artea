# npm / pnpm / yarn client setup

Artea exposes one URL for everything: `http://localhost:8080` (substitute your
deployment's host). Two npm registries live behind it:

| Purpose | URL | Backed by |
|---------|-----|-----------|
| Public packages (pull-through cache of npmjs.org) | `http://localhost:8080/npm/` | Verdaccio |
| Private `@artea/*` packages (publish + install) | `http://localhost:8080/api/packages/artea/npm/` | Gitea |

The npm client itself does the routing: anything in the `@artea` scope goes to
Gitea, everything else goes to the cache. There is no anonymous access anywhere.

## 1. Get a personal access token (PAT)

1. Sign in to `http://localhost:8080` (via Okta SSO if configured — see
   [okta.md](okta.md)).
2. Avatar menu → **Settings** → **Applications** (`/user/settings/applications`).
3. Under *Manage Access Tokens*: enter a token name, expand **Select permissions**,
   and set **package** to:
   - **Read** (`read:package`) — install only
   - **Read and Write** (`write:package`) — install *and* publish
4. Click **Generate Token** and copy it immediately — it is shown only once.

One token is enough for everything (install, publish, npm, pip, twine). See
[publishing.md](publishing.md) for the scope model.

## 2. Configure npm — the `.npmrc`

Put this in `~/.npmrc` (per-user) or your project's `.npmrc`:

```ini
registry=http://localhost:8080/npm/
@artea:registry=http://localhost:8080/api/packages/artea/npm/
//localhost:8080/npm/:_auth=<base64 user:PAT>
//localhost:8080/api/packages/artea/npm/:_authToken=<PAT>
always-auth=true
```

- `<base64 user:PAT>` — generate with:

  ```sh
  echo -n 'your-username:your-token' | base64
  ```

- `<PAT>` — the raw token. Gitea accepts it as a Bearer token
  (`_authToken`), which is what npm sends.
- npm ≥ 9 ignores `always-auth` (URL-scoped credentials are always sent);
  keeping the line is harmless and required for older clients.

### Verify

```sh
npm ping --registry http://localhost:8080/npm/   # cache reachable + auth ok
npm view left-pad versions                        # public, via pull-through
```

## 3. Publish (private packages only)

Only the `@artea` scope is publishable, and it publishes to Gitea — the public
cache is read-only by configuration, so `npm publish` of an unscoped package
fails by design.

```jsonc
// package.json
{
  "name": "@artea/hello-artea",
  "version": "1.0.0"
}
```

```sh
npm publish
```

The npm client routes the publish (an HTTP `PUT`) to the `@artea:registry` URL,
i.e. straight to Gitea's npm endpoint `/api/packages/artea/npm/` — the same URL
installs use, with the same token. Requires a `write:package` token and
membership in the `artea` org with package write permission.

## 4. Install

```sh
npm install @artea/hello-artea   # private, from Gitea (scope routing)
npm install left-pad             # public, via Verdaccio pull-through of npmjs.org
```

Private `@artea/*` requests never touch the public cache or npmjs.org; public
requests are filtered by the org policy (`npm-rules.yaml` in
`artea/registry-policy`) — blocked names/versions simply disappear from metadata.

## 5. pnpm

pnpm reads the same `.npmrc` (project-level and `~/.npmrc`) — no changes
needed. Equivalent imperative setup:

```sh
pnpm config set registry http://localhost:8080/npm/
pnpm config set @artea:registry http://localhost:8080/api/packages/artea/npm/
pnpm config set //localhost:8080/npm/:_auth $(echo -n 'user:PAT' | base64)
pnpm config set //localhost:8080/api/packages/artea/npm/:_authToken PAT
pnpm config set always-auth true
```

## 6. yarn

**Yarn 1.x (classic)** reads `.npmrc`; the configuration above works as-is.

**Yarn 2+ (Berry)** uses `.yarnrc.yml` instead:

```yaml
npmRegistryServer: "http://localhost:8080/npm/"
npmAlwaysAuth: true
npmAuthIdent: "<base64 user:PAT>"   # same value as the _auth line in .npmrc

npmScopes:
  artea:
    npmRegistryServer: "http://localhost:8080/api/packages/artea/npm/"
    npmAlwaysAuth: true
    npmAuthToken: "<PAT>"

# Yarn Berry refuses to send credentials over plain http unless whitelisted.
# Only needed for local-dev http; not for a TLS-terminated deployment.
unsafeHttpWhitelist:
  - localhost
```

## Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| `401` on any install | Missing/expired token, `_auth` not base64-encoded, or credentials line doesn't match the registry URL prefix |
| `401` on publish | Token is `read:package` only, or user lacks write permission in the `artea` org |
| Publish rejected on `/npm/` | Expected: the cache is read-only; only `@artea/*` (→ Gitea) is publishable |
| `404` for `@artea/*` | `@artea:registry` line missing — the request went to the public cache, which denies the private scope by design |
| Public package missing versions | Blocked by policy (`npm-rules.yaml`) — intentional |

See also [operations.md](operations.md) for the operator-side view.
