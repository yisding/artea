# Publishing, tokens, and CI

## The one-token model

Every credential in Artea is a Gitea **personal access token (PAT)**. The same
token works on every surface — Gitea's package endpoints, the npm pull-through
cache, and the PyPI gateway paths — because all of them validate credentials
against Gitea (`GET /api/v1/user`). There are no separate registry accounts.

PATs are currently non-expiring (long-lived credentials are a v1 requirement);
revoke them in **Settings → Applications** when no longer needed. Revocation
takes effect within 60 seconds everywhere (see
[operations.md](operations.md#pat-revocation)).

## Token scopes

Gitea's scope for packages is the `package` permission category:

| Scope | UI label | Allows |
|-------|----------|--------|
| `read:package` | package: Read | install/download from all registries |
| `write:package` | package: Read and Write | publish **and** install — write implies read |

So: one `write:package` token both publishes and consumes (requirement R6);
hand out `read:package` tokens to anything that only installs (CI build jobs,
developer machines that never publish).

> Note: the architecture document refers to these as `read:packages` /
> `write:packages`; Gitea's actual scope identifiers are singular
> (`read:package`, `write:package`). Use the singular form anywhere a literal
> scope string is required (API calls, the CLI below).

A token alone is not sufficient to publish: the user must also be a member of
the `artea` organization with write permission on its packages — normally via
the `developers` team (see below). A `read:package` token — or a
`write:package` token of a user without org write access — gets
`401 Unauthorized` from Gitea on publish.

## Org roles and governance

Bootstrap creates the org teams; humans are never made org **Owners** (that
team is reserved for `artea-admin`):

| Team | Access | Who |
|------|--------|-----|
| `developers` | code + pulls + packages **write** (no admin), all org repos | everyone who publishes packages or edits policy (e.g. the demo user `dev1`) |
| `policy-readers` | code **read** on `artea/registry-policy` only | service accounts; holds `svc-policy`, whose `read:repository` PAT is what policy-sync uses |
| `Owners` | org admin | `artea-admin` only |

Registry policy is enforceably PR-only: the default branch of
`artea/registry-policy` carries branch protection — direct pushes are blocked
for everyone except `artea-admin`, and merging requires at least one approval
(e2e scenario S14). Developers change policy by pushing a branch and opening a
pull request; see [ADR-0006](../adr/0006-policy-as-code.md).

Tokens are sent as:

- HTTP Basic, `username:token` (npm `_auth`, pip/netrc, twine, uv, poetry) —
  this is the whole npm client contract now; Gitea accepts Basic with the PAT
  as the password
- `Authorization: token <PAT>` or `Authorization: Bearer <PAT>` on Gitea paths
  (the legacy npm `_authToken` form uses Bearer; still accepted, no longer
  needed)

## Creating tokens

**Humans**: web UI — avatar → **Settings** → **Applications**, name the token,
set the **package** permission, generate, copy once. This is the only flow for
SSO users (they have no password, and the token REST API requires Basic auth).

**Service accounts** (CI bots): an admin can mint a token from the CLI without
ever setting a password:

```sh
docker compose exec -u git gitea \
  gitea admin user generate-access-token \
    --username ci-bot --token-name ci-publish \
    --scopes write:package --raw
```

(`--raw` prints just the token; create the `ci-bot` user first and add it to
the `developers` team — never to Owners. Give read-only bots like policy-sync's
`svc-policy` only the narrowest team and scope they need.)

## What publishes where

| Action | Endpoint | Token scope |
|--------|----------|-------------|
| `npm publish` of `@artea/*` | `PUT http://localhost:8080/npm/@artea%2f<name>` — the gateway routes the scope server-side to Gitea's `/api/packages/artea/npm/` | `write:package` |
| `twine upload` | `POST http://localhost:8080/api/packages/artea/pypi/` | `write:package` |
| `npm install`, `pip install` (private or public) | `http://localhost:8080/...` (see client guides) | `read:package` |

The pull-through caches are read-only: publishing unscoped npm packages or
uploading to the `/pypi/simple/` index is rejected by design. Gitea is the only
writable surface.

## CI examples

Store two secrets in your CI system: `ARTEA_USER` (the bot username) and
`ARTEA_TOKEN` (its PAT). Examples use GitHub-Actions syntax; the shell steps
are CI-agnostic.

### npm publish

```yaml
jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 22
      - name: Configure registry
        run: |
          AUTH=$(printf '%s:%s' "${{ secrets.ARTEA_USER }}" "${{ secrets.ARTEA_TOKEN }}" | base64 -w0)
          cat > .npmrc <<EOF
          registry=http://localhost:8080/npm/
          //localhost:8080/:_auth=${AUTH}
          //localhost:8080/npm/:_auth=${AUTH}
          always-auth=true
          EOF
      - run: npm publish
```

(Two `_auth` lines, same value: the host-rooted one covers installs including
Gitea-generated tarball URLs; the exact-registry one satisfies npm's local
publish preflight — see [clients-npm.md](clients-npm.md).)

### npm install (consume only)

```yaml
      - name: Configure registry
        run: |
          AUTH=$(printf '%s:%s' "${{ secrets.ARTEA_USER }}" "${{ secrets.ARTEA_TOKEN }}" | base64 -w0)
          cat > .npmrc <<EOF
          registry=http://localhost:8080/npm/
          //localhost:8080/:_auth=${AUTH}
          always-auth=true
          EOF
      - run: npm ci
```

### Python build + twine upload

```yaml
jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install build twine
      - run: python -m build
      - name: Upload to Artea
        env:
          TWINE_REPOSITORY_URL: http://localhost:8080/api/packages/artea/pypi/
          TWINE_USERNAME: ${{ secrets.ARTEA_USER }}
          TWINE_PASSWORD: ${{ secrets.ARTEA_TOKEN }}
        run: twine upload dist/*
```

### pip install (consume only)

```yaml
      - name: Install via Artea
        env:
          PIP_INDEX_URL: http://${{ secrets.ARTEA_USER }}:${{ secrets.ARTEA_TOKEN }}@localhost:8080/pypi/simple/
        run: pip install -r requirements.txt
```

(For long-lived runners prefer a `~/.netrc` written from secrets over the
URL-embedded form, so the token never appears in process listings or logs.)

Replace `localhost:8080` with your deployment's public base URL in all of the
above; CI runners must be able to reach the gateway.
