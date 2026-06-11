# verdaccio-filter-artea

One package, two Verdaccio plugin roles, enforcing Artea's npm block policy
(requirement R3). Wire it under **both** `filters:` and `middlewares:`:

- **Metadata filter** (`IPluginStorageFilter`, `filter_metadata` hook): rewrites
  packuments before Verdaccio serves them. Blocked versions disappear from
  `versions`/`time`, dist-tags pointing at removed versions are dropped (`latest` is
  re-pointed to the highest remaining version), and fully-blocked names are served
  with zero versions so installs fail with the standard "No matching version found"
  npm error.
- **Middleware** (`IPluginMiddleware`, `register_middlewares` hook): intercepts
  tarball downloads — `GET /{pkg}/-/{file}.tgz`, scoped
  `GET /@{scope}/{pkg}/-/{file}.tgz`, and their URL-encoded variants (`%2f`, `%40`) —
  and answers `403` with a JSON error for blocked names, scopes, and versions.
  Metadata filtering alone can be bypassed by constructing the tarball URL directly
  (e2e scenario S13); the middleware closes that hole. The version is derived from
  the filename by stripping the exact `<unscoped-name>-` prefix, so hyphenated names
  and prerelease/build-metadata versions are handled.

Both roles share one policy-loading code path (`src/policy.ts`), with two
interchangeable policy sources behind the same `PolicyLoader` interface:

- **`policy_file`** — read from disk, re-parsed when the mtime changes. Use in
  docker compose, where policy-sync and Verdaccio share the `policy-data` volume.
- **`policy_url`** — polled over HTTP with ETag/If-None-Match from policy-sync's
  `GET /policy/npm-rules.yaml`. Use in Kubernetes, where there is no shared
  (RWX) volume.

Exactly **one** of the two must be configured; setting both or neither is a
startup error.

## Configuration (verdaccio config.yaml)

File mode (compose):

```yaml
filters:
  filter-artea:
    policy_file: /policy/npm-rules.yaml   # the shared policy-data volume

middlewares:
  filter-artea:                           # same package, middleware role (S13)
    policy_file: /policy/npm-rules.yaml
    # fail_open: true                     # escape hatch, see below; default false
```

URL mode (K8s):

```yaml
filters:
  filter-artea:
    policy_url: http://policy-sync:8920/policy/npm-rules.yaml
    # poll_interval_ms: 10000             # default; poll period
    # fail_grace_ms: 60000                # default; failure window before fail-closed

middlewares:
  filter-artea:
    policy_url: http://policy-sync:8920/policy/npm-rules.yaml
```

Verdaccio instantiates the package once per role, so the filter and the
middleware each run their own poller — two policy GETs per `poll_interval_ms`,
which the ETag handling turns into cheap 304s.

| Key | Default | Meaning |
|-----|---------|---------|
| `policy_file` | — | Policy file path (compose). Mutually exclusive with `policy_url` |
| `policy_url` | — | policy-sync endpoint to poll (K8s). Mutually exclusive with `policy_file` |
| `poll_interval_ms` | `10000` | URL mode only: poll period |
| `fail_grace_ms` | `60000` | URL mode only: how long polls may keep failing before fail-closed |
| `fail_open` | `false` | Escape hatch: never reject, see below |

## Policy file schema (`npm-rules.yaml`)

The file lives in the Gitea repo `artea/registry-policy`; policy-sync writes it
into the `/policy` volume (compose) and serves it at `GET /policy/npm-rules.yaml`
(K8s). Top-level key `blocked` with two optional lists:

```yaml
blocked:
  # Block every package in a scope. Leading "@" is optional.
  scopes:
    - "@evil-corp"

  # Block individual packages.
  packages:
    # Bare string: block ALL versions of the package.
    - event-stream

    # Mapping form. "versions" is an optional semver RANGE (the `semver` npm
    # package syntax: ">=1.2.0 <2", "~4.17.0", "1.3.0", "<2 || >3", ...).
    # Omitting "versions" blocks all versions. "reason" is free text for humans.
    - name: left-pad
      versions: "1.3.0"
      reason: "example: block a single version"
    - name: lodash
      versions: "<4.17.21"
      reason: "CVE-2021-23337"
```

Semantics:

- Multiple `packages` entries for the same name are OR-ed together.
- Range matching uses `{ includePrerelease: true, loose: true }`, so `<2` also blocks
  `2.0.0-beta`-style prereleases of blocked ranges — a blocklist should over-block.
- Entries that are malformed (missing `name`, invalid semver range, non-string scope)
  are skipped with a warning; the rest of the file still applies.
- An empty file, or a file without `blocked`, is an empty policy.

## Reload behavior

- **File mode**: the plugin `stat()`s the policy file on every request and re-parses
  it only when the mtime changes — no restart needed after policy-sync writes a new
  file. A stale-but-valid file keeps serving as last-known-good: freshness is
  policy-sync's job, not the plugin's.
- **URL mode**: a background poller GETs `policy_url` every `poll_interval_ms`
  (default 10s) sending `If-None-Match` with the last seen ETag. A `200` parses and
  atomically swaps the active policy; a `304` keeps it. The ETag of an unparsable
  body is never adopted, so a later `304` can never mask a fix. Worst-case policy
  propagation is one poll interval after policy-sync has synced.

## Failure mode: fail-closed (default)

Rejection always looks the same (e2e scenario S15):

- the middleware answers tarball requests with
  `503 {"error": "policy unavailable: ..."}`;
- the filter strips every version from every packument it sees, so installs fail.
  (It cannot 503: Verdaccio swallows errors thrown by filter plugins and would serve
  the packument unfiltered — stripping is the only reliable rejection.)

When rejection kicks in differs per mode:

- **File mode**: immediately, whenever the file is **missing or unparsable**
  (invalid YAML or wrong structure). The failed state clears automatically through
  the same mtime/stat reload: as soon as the file reappears or is fixed, the policy
  applies again — no restart.
- **URL mode**: transient failures are normal on a network, so the last
  successfully fetched policy keeps serving (last-known-good, in memory) while
  polls fail — whether the failure is a connection error, a non-2xx response, or an
  unparsable body. Only when failures persist past `fail_grace_ms` (default 60s)
  does the plugin fail closed — plus on **cold start**, when nothing has ever been
  fetched (there is no known-good policy to serve). The first successful poll
  recovers automatically.

Load failures and the open/closed transitions are logged once per transition
(`warn`/`error` level); rejected requests log at `warn`.

### `fail_open: true` (escape hatch, not advised)

Never rejects, in either mode: a missing policy source is treated as an empty
policy (nothing blocked) and a broken update keeps the last good policy in effect
indefinitely. Only use this if availability of public packages matters more than
policy enforcement.

## Develop

```sh
pnpm install        # from verdaccio/plugins/
pnpm build          # tsc -> dist/ (CommonJS, what verdaccio 6 loads)
pnpm test           # vitest
```
