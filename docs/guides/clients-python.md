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
authenticate = "always"
```

`authenticate = "always"` (uv ≥ 0.6.9) makes uv attach credentials to the
first request instead of trying anonymously, getting a `401`, and retrying —
Artea requires auth on every request, so the anonymous attempt is always a
wasted round-trip.

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

`--priority=default` (full PyPI replacement) is deliberate: `primary` still
falls back to public PyPI, which forfeits the dependency-confusion guarantee.

**Poetry 2.x + PEP 621:** with a `[project]`-table `pyproject.toml`,
`poetry source add` can exit successfully *without* writing the source
(observed with poetry 2.4.1) — `poetry add` then resolves against public PyPI
and 404s on private packages. After running it, check that `pyproject.toml`
contains the block below, and add it by hand if missing:

```toml
[[tool.poetry.source]]
name = "artea"
url = "http://localhost:8080/pypi/simple/"
priority = "default"
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

### Upload-time / release-age filtering

The gateway serves the **PEP 700 JSON Simple API (`api-version` `1.1`)** with a
per-file `upload-time` whenever a client sends
`Accept: application/vnd.pypi.simple.v1+json` — for **both** public
(devpi pull-through) and private (Gitea) packages. Time-based install filters
therefore work through the Artea index:

```sh
pip install --uploaded-prior-to 2026-01-01T00:00:00Z six   # pip 25.1+
uv pip install --exclude-newer 2026-01-01 six              # uv
# poetry: package-time-filtering / min-release-age (Poetry 2.x) reads upload-time too
```

Notes:

- This is *additive* to the server-side `upstream.min_age` gate compiled from
  `policy.toml`: a public file that is still too new under that policy
  is already absent from the index, so a client-side time filter composes with
  it rather than overriding it.
- Public files carry the exact `upload-time` PyPI reports (microsecond UTC),
  plus the PyPI-reported `size`. Private (Gitea) files carry their **version's**
  upload time (Gitea records upload time per version, so all files in a version
  share it) plus the per-file `size` from Gitea's package-files API. This is
  coarser than per-file: for a version published atomically (a normal
  `twine`/`uv` upload) it equals the real upload time, but a file *added to an
  existing version later* inherits that version's earlier timestamp, so a
  `--uploaded-prior-to` cutoff could select it even if its own upload was after
  the cutoff. Rare given the publish model, but not an absolute guarantee; for a
  hard per-file age bound, lean on the server-side `upstream.min_age` gate too.
- Availability over metadata: `upload-time` is spec-optional, so a transient
  upstream-metadata blip never breaks a plain install. If the base index list is
  reachable but the upstream upload-time source is momentarily down (and no
  recent enriched copy is cached), the gateway serves the still-installable v1.1
  list *without* the time stamps rather than failing the request. A time-filter
  client simply won't match the un-stamped files (the safe direction); a plain
  `pip/uv install` is unaffected. Only an unreachable **base index** returns an
  error.
- A plain request (no special `Accept`) is unchanged: `pip install` still gets
  the PEP 503 HTML / PEP 691 v1.0 page exactly as before.

### Faster resolution (PEP 658/714 metadata)

For **public** (pull-through) packages the index advertises **PEP 658/714 Core
Metadata**: each wheel carries `data-core-metadata` (HTML) / `core-metadata`
(JSON), and the wheel's `METADATA` is downloadable at `<wheel-url>.metadata`.
pip and uv use this to resolve dependencies by fetching a few KB of metadata
per candidate instead of downloading whole wheels — noticeably faster locking
and back-tracking. It needs no client flags (pip ≥ 22.3 / modern uv use it
automatically). A blocked or too-new wheel's metadata is gated exactly like the
wheel, so it never leaks past policy.

**Private** (Gitea-hosted) packages do not yet expose PEP 658 metadata — Gitea's
PyPI registry serves no `.metadata` file — so resolvers fall back to downloading
the wheel for those, which is fine for the small private packages this targets.

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
| A public package/version refuses to install | Blocked by a `deny` rule in `policy.toml` or still too new under `upstream.min_age` — intentional |

See also [operations.md](operations.md).
