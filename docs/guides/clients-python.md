# pip / uv / poetry / twine client setup

Python tooling uses a **single index URL** for both private and public packages:

```
http://localhost:8080/pypi/simple/
```

(substitute your deployment's host). The gateway checks Gitea first for every
package name; only if Gitea answers 404 does the request fall through to the
PyPI pull-through cache. A privately published name therefore fully shadows any
same-named public package — clients never see public versions of a private name.
Private packages live under the configured Gitea namespace
(`ARTEA_NAMESPACE`, default `artea`).

Uploads go directly to Gitea's PyPI endpoint, never to the cache:

```
http://localhost:8080/api/packages/<namespace>/pypi/
```

Replace `<namespace>` with your `ARTEA_NAMESPACE`.

There is no anonymous access. Credentials are your Gitea username plus a
personal access token (PAT), from either a manually created Gitea account or an
Okta-backed account; see step 1 of [clients-npm.md](clients-npm.md) for how to
create one. Install tokens need `read:package`; publish tokens need
`write:package`; both also need `read:user` and `read:organization` so the
gateway and npm cache can validate the same credential. See
[publishing.md](publishing.md) for the full scope model.

## 1. Credentials via `~/.netrc`

All the tools below speak HTTP Basic and respect `~/.netrc`, so configure
credentials once:

```
machine localhost
login your-username
password your-token
```

```sh
chmod 600 ~/.netrc
```

For a real deployment replace `localhost` with the registry host. (Embedding
credentials in the index URL — `http://user:PAT@host/pypi/simple/` — also works
but leaks the token into logs and lockfiles; prefer netrc.)

## 2. pip

`pip.conf` (`~/.config/pip/pip.conf` on Linux,
`~/Library/Application Support/pip/pip.conf` on macOS, `%APPDATA%\pip\pip.ini`
on Windows):

```ini
[global]
index-url = http://localhost:8080/pypi/simple/
```

Or per-shell: `export PIP_INDEX_URL=http://localhost:8080/pypi/simple/`.

```sh
pip install six           # public, via devpi pull-through of pypi.org
pip install acme-hello    # private example when ARTEA_NAMESPACE=acme
pip index versions acme-hello    # shows ONLY private versions for private names
```

Do **not** add `extra-index-url` entries pointing at pypi.org — that would
bypass the precedence guarantee and reintroduce dependency-confusion risk.

## 3. uv

In `uv.toml` (`~/.config/uv/uv.toml`) or under `[tool.uv]` in `pyproject.toml`:

```toml
[[index]]
name = "artea"
url = "http://localhost:8080/pypi/simple/"
default = true
```

Credentials: uv reads `~/.netrc` automatically; alternatively use the
per-index environment variables:

```sh
export UV_INDEX_ARTEA_USERNAME=your-username
export UV_INDEX_ARTEA_PASSWORD=your-token
```

## 4. poetry

```sh
poetry source add --priority=default artea http://localhost:8080/pypi/simple/
poetry config http-basic.artea your-username your-token
```

`poetry add six` and `poetry add acme-hello` then both resolve through the
gateway with the same precedence guarantees.

## 5. Publishing with twine

Build as usual (`python -m build`), then upload **to Gitea directly** — wheels
and sdists are stored in Gitea, never in the cache. `~/.pypirc`:

```ini
[distutils]
index-servers =
    artea

[artea]
repository = http://localhost:8080/api/packages/<namespace>/pypi/
username = your-username
password = your-token
```

```sh
twine upload -r artea dist/*
```

Requires a token with `write:package`, `read:user`, and `read:organization`,
plus write permission in the configured namespace org. `.pypirc` does not
expand environment variables; substitute the actual namespace in the URL.
Or without a `.pypirc`:

```sh
TWINE_REPOSITORY_URL=http://localhost:8080/api/packages/${ARTEA_NAMESPACE}/pypi/ \
TWINE_USERNAME=your-username \
TWINE_PASSWORD=your-token \
twine upload dist/*
```

### Name normalization

Gitea normalizes package names per PEP 503 (`.` and `_` become `-`), so
`my_package`, `my.package`, and `my-package` are the same project. Install
using any spelling; the index entry is the normalized one.

## Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| `401` on install | netrc missing/typo'd, wrong machine name, revoked token, or missing `read:user` / `read:organization` |
| `401`/`403` on `twine upload` | Token is `read:package` only, missing the supporting scopes, or user lacks write permission in the configured namespace org |
| `404` for a private package | Not published yet, or name-normalization mismatch — check `http://localhost:8080/pypi/simple/<normalized-name>/` |
| pip resolves a *public* version of a private name | Should never happen — check for stray `extra-index-url` config on the client; if absent, report it (gateway precedence bug) |
| A public package/version refuses to install | Blocked by `pypi-constraints.txt` or still too new under `upstream-policy.yaml` — intentional |

See also [operations.md](operations.md).
