# npm / pnpm / yarn client setup

Artea exposes one URL for everything: `http://localhost:8080` (substitute your
deployment's host). The private scope is `@${ARTEA_NAMESPACE}` (default
`@artea`; use the value from your deployment's `.env`). npm clients configure a
**single registry**:

| Purpose | URL | Backed by |
|---------|-----|-----------|
| Everything npm — public installs, private `@${ARTEA_NAMESPACE}/*` install + publish | `http://localhost:8080/npm/` | Verdaccio (pull-through cache of npmjs.org) for public names; Gitea for the configured private scope |

The gateway does the scope routing server-side: any configured private-scope
request under `/npm/` — packuments, tarballs, publishes, and the dist-tag API
under `/-/package/` — is proxied to Gitea's npm endpoint; everything else goes
to the cache. The client routes nothing, so a client missing scope
configuration can no longer leak or misroute private names. There is no
anonymous access anywhere.

## 1. Get a personal access token (PAT)

1. Sign in to `http://localhost:8080` (via Okta SSO if configured — see
   [okta.md](okta.md)).
2. Avatar menu → **Settings** → **Applications** (`/user/settings/applications`).
3. Under *Manage Access Tokens*: enter a token name, expand **Select permissions**,
   and set these permissions:
   - **user**: **Read** (`read:user`), required by the gateway auth guard
   - **organization**: **Read** (`read:organization`), required for Verdaccio
     org/group mapping
   - **package**: **Read** (`read:package`) for install-only tokens, or
     **Read and Write** (`write:package`) for install-and-publish tokens
4. Click **Generate Token** and copy it immediately — it is shown only once.

One token is enough for everything (install, publish, npm, pip, twine), but it
must include `read:user`, `read:organization`, and either `read:package` or
`write:package`. See [publishing.md](publishing.md) for the scope model.

## 2. Configure npm — the `.npmrc`

Put this in `~/.npmrc` (per-user) or your project's `.npmrc`:

```ini
registry=http://localhost:8080/npm/
//localhost:8080/:_auth=<base64 user:PAT>
always-auth=true
```

- `<base64 user:PAT>` — generate with:

  ```sh
  echo -n 'your-username:your-token' | base64
  ```

- npm ≥ 9 ignores `always-auth` (URL-scoped credentials are always sent);
  keeping the line is harmless and required for older clients.

**If you publish**, add one more line carrying the *same* value (the reason is
under [Publish](#3-publish-private-packages-only) below):

```ini
//localhost:8080/npm/:_auth=<base64 user:PAT>
```

### Why the host-rooted credential line covers all installs

npm matches credentials to request URLs by URL prefix ("nerf-darts"): a
`//localhost:8080/:` line applies to every path on that host and port. That
matters because private tarballs do not download from `/npm/...` — Gitea
builds the tarball URLs in its packuments from its `ROOT_URL`
(`http://localhost:8080/`), so they live under
`/api/packages/${ARTEA_NAMESPACE}/npm/...`
on the same host. The single host-rooted line covers `/npm/` and the Gitea
tarball paths alike; only publishing needs the second line (below).

`_auth` (HTTP Basic, `user:PAT`) now works against both backends: the cache
side validates the credential against Gitea, and Gitea itself accepts Basic
with the PAT as the password. The Bearer-style `_authToken=<PAT>` still works
on the private-scope routes but is no longer needed.

### Verify

```sh
npm ping --registry http://localhost:8080/npm/   # cache reachable + auth ok
npm view left-pad versions                        # public, via pull-through
```

### Previous configuration

The old two-URL setup — an extra
`@${ARTEA_NAMESPACE}:registry=http://localhost:8080/api/packages/${ARTEA_NAMESPACE}/npm/`
line with its own `_authToken` credential — **keeps working unchanged** for the
configured namespace: the Gitea endpoint is still served at that URL, and
Verdaccio still denies `@${ARTEA_NAMESPACE}/*` as defense in depth. Existing
`.npmrc` files need no migration; use the single-registry form above for new
setups.

## 3. Publish (private packages only)

Only the configured private scope is publishable, and it lands in Gitea — the
public cache is read-only by configuration, so `npm publish` of an unscoped
package fails by design. For example, with `ARTEA_NAMESPACE=acme`:

```jsonc
// package.json
{
  "name": "@acme/hello-acme",
  "version": "1.0.0"
}
```

```sh
npm publish
```

The publish is an HTTP `PUT` to the configured registry
(`http://localhost:8080/npm/@acme%2fhello-acme` in the example); the gateway
routes it to Gitea's npm endpoint server-side — the same path installs take,
with the same token. Requires a `write:package` token and membership in the
configured namespace org with package write permission.

**Publishing needs the second `_auth` line.** Before sending any request, npm
runs a local credential preflight that checks only the *exact* nerf-dart of
the registry (`//localhost:8080/npm/:`) — unlike its request-time matching, it
never walks up to `//localhost:8080/:` (verified with npm 11.16.0). With only
the host-rooted line, `npm publish` fails `ENEEDAUTH` before anything is sent.
Hence the contract: one credential value on two nerf-dart lines.

## 4. Install

```sh
npm install @acme/hello-acme     # private, routed to Gitea by the gateway
npm install left-pad             # public, via Verdaccio pull-through of npmjs.org
```

Private `@${ARTEA_NAMESPACE}/*` requests never touch the public cache or
npmjs.org — the gateway peels the scope off before Verdaccio, which
additionally denies it by configuration. Public requests are filtered by the
org policy in `${ARTEA_NAMESPACE}/registry-policy`: `npm-rules.yaml` blocks
names/versions, and `upstream-policy.yaml` hides versions younger than the
shared `upstream.min_age` duration.

## 5. pnpm

pnpm reads the same `.npmrc` (project-level and `~/.npmrc`) — no changes
needed. Equivalent imperative setup:

```sh
pnpm config set registry http://localhost:8080/npm/
pnpm config set //localhost:8080/:_auth $(echo -n 'user:PAT' | base64)
pnpm config set always-auth true
# only if you publish (npm's publish preflight, see above):
pnpm config set //localhost:8080/npm/:_auth $(echo -n 'user:PAT' | base64)
```

## 6. yarn

**Yarn 1.x (classic)** reads `.npmrc`; the configuration above works as-is.

**Yarn 2+ (Berry)** uses `.yarnrc.yml` instead — and since the gateway routes
the configured private scope, no `npmScopes` block is needed:

```yaml
npmRegistryServer: "http://localhost:8080/npm/"
npmAlwaysAuth: true
npmAuthIdent: "<base64 user:PAT>"   # same value as the _auth line in .npmrc

# Yarn Berry refuses to send credentials over plain http unless whitelisted.
# Only needed for local-dev http; not for a TLS-terminated deployment.
unsafeHttpWhitelist:
  - localhost
```

## Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| `401` on any install | Missing/expired token, missing `read:user` / `read:organization`, `_auth` not base64-encoded, or credentials line doesn't match the registry URL prefix |
| `401` on publish | Token is `read:package` only, missing the supporting scopes, or user lacks write permission in the configured namespace org |
| `ENEEDAUTH` on publish, no request sent | The `//localhost:8080/npm/:_auth` line is missing — npm's publish preflight checks only the exact registry nerf-dart, never the host-rooted line |
| Publish of an unscoped package rejected | Expected: the cache is read-only; only `@${ARTEA_NAMESPACE}/*` (→ Gitea) is publishable |
| `404` for `@${ARTEA_NAMESPACE}/*` | The package or version is not published — the gateway routes the scope server-side, so a missing client scope line is no longer a cause (legacy scope registry configs also still work) |
| Public package missing versions | Blocked by `npm-rules.yaml` or still too new under `upstream-policy.yaml` — intentional |

See also [operations.md](operations.md) for the operator-side view.
