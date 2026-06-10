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
the `artea` organization with write permission on its packages (org owners, or
a team with package write access). A `read:package` token — or a
`write:package` token of a user without org write access — gets
`401 Unauthorized` from Gitea on publish.

Tokens are sent as:

- HTTP Basic, `username:token` (npm `_auth`, pip/netrc, twine, uv, poetry)
- `Authorization: token <PAT>` or `Authorization: Bearer <PAT>` on Gitea paths
  (npm `_authToken` uses Bearer)

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

(`--raw` prints just the token; create the `ci-bot` user and add it to the
`artea` org first.)

## What publishes where

| Action | Endpoint | Token scope |
|--------|----------|-------------|
| `npm publish` of `@artea/*` | `PUT http://localhost:8080/api/packages/artea/npm/` (routed by the npm client via `@artea:registry`) | `write:package` |
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
          cat > .npmrc <<EOF
          @artea:registry=http://localhost:8080/api/packages/artea/npm/
          //localhost:8080/api/packages/artea/npm/:_authToken=${{ secrets.ARTEA_TOKEN }}
          EOF
      - run: npm publish
```

### npm install (consume only)

```yaml
      - name: Configure registry
        run: |
          AUTH=$(printf '%s:%s' "${{ secrets.ARTEA_USER }}" "${{ secrets.ARTEA_TOKEN }}" | base64 -w0)
          cat > .npmrc <<EOF
          registry=http://localhost:8080/npm/
          @artea:registry=http://localhost:8080/api/packages/artea/npm/
          //localhost:8080/npm/:_auth=${AUTH}
          //localhost:8080/api/packages/artea/npm/:_authToken=${{ secrets.ARTEA_TOKEN }}
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
