# verdaccio-filter-artea

One package, two Verdaccio plugin roles, enforcing Artea's npm block/age policy
(requirement R3). Wire it under **both** `filters:` and `middlewares:`:

- **Metadata filter** (`IPluginStorageFilter`, `filter_metadata` hook): rewrites
  packuments before Verdaccio serves them. Blocked or too-new versions disappear
  from `versions`/`time`, dist-tags pointing at removed versions are dropped
  (`latest` is re-pointed to the highest remaining version), and fully-blocked
  names are served with zero versions so installs fail with the standard
  "No matching version found" npm error.
- **Middleware** (`IPluginMiddleware`, `register_middlewares` hook): intercepts
  tarball downloads — `GET /{pkg}/-/{file}.tgz`, scoped
  `GET /@{scope}/{pkg}/-/{file}.tgz`, and their URL-encoded variants (`%2f`, `%40`) —
  and answers `403` with a JSON error for blocked names, scopes, versions, and
  versions younger than the configured upstream age.
  Metadata filtering alone can be bypassed by constructing the tarball URL directly
  (e2e scenario S13); the middleware closes that hole. The version is derived from
  the filename by stripping the exact `<unscoped-name>-` prefix, so hyphenated names
  and prerelease/build-metadata versions are handled. If an age-gated direct
  tarball arrives before Verdaccio has served the package metadata, the middleware
  fetches the npm packument from `npm_registry_url` (default
  `https://registry.npmjs.org`) to verify the publish time and fails closed on
  lookup errors.

Both roles share one policy-loading code path (`src/policy.ts`): a `stat()` per
request, re-parse only on mtime change.

## Configuration (verdaccio config.yaml)

```yaml
filters:
  filter-artea:
    policy_file: /policy/npm-rules.yaml   # default; the shared policy-data volume
    npm_registry_url: https://registry.npmjs.org

middlewares:
  filter-artea:                           # same package, middleware role (S13)
    policy_file: /policy/npm-rules.yaml
    npm_registry_url: https://registry.npmjs.org
```

## Policy file schema (`npm-rules.yaml`)

The file lives in the configured Gitea policy repo
`${ARTEA_NAMESPACE}/registry-policy` (or explicit `POLICY_REPO`) and is written
into the `/policy` volume by policy-sync. Top-level key `blocked` with two
optional lists and an optional upstream-age gate:

```yaml
upstream:
  # Hide/reject public versions until they have been upstream for at least this
  # long. Units: ms, s, m, h, d. Use 0d or omit to disable.
  min_age: 3d

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
- `upstream.min_age` accepts `ms`, `s`, `m`, `h`, or `d` durations. When it is
  active, a version without a parseable `time[version]` publish timestamp is
  treated as blocked.
- Range matching uses `{ includePrerelease: true, loose: true }`, so `<2` also blocks
  `2.0.0-beta`-style prereleases of blocked ranges — a blocklist should over-block.
- Entries that are malformed (missing `name`, invalid semver range, non-string scope)
  are skipped with a warning; the rest of the file still applies.
- An empty file, or a file without `blocked`, is an empty policy.

## Reload behavior

The plugin `stat()`s the policy file on every request and re-parses it only when the
mtime changes — no restart needed after policy-sync writes a new file. A
stale-but-valid file keeps serving as last-known-good: freshness is policy-sync's
job, not the plugin's.

## Failure mode: fail-closed (default)

If the policy file is **missing or unparsable** (invalid YAML or wrong structure),
the plugin rejects public-package traffic instead of silently serving everything
unfiltered (e2e scenario S15):

- the middleware answers tarball requests with
  `503 {"error": "policy unavailable: ..."}`;
- the filter strips every version from every packument it sees, so installs fail.
  (It cannot 503: Verdaccio swallows errors thrown by filter plugins and would serve
  the packument unfiltered — stripping is the only reliable rejection.)

The failed state clears automatically through the same mtime/stat reload: as soon as
the file reappears or is fixed, the policy applies again — no restart. Load failures
are logged once per transition (`error` level); rejected requests log at `warn`.

## Develop

```sh
pnpm install        # from verdaccio/plugins/
pnpm build          # tsc -> dist/ (CommonJS, what verdaccio 6 loads)
pnpm test           # vitest
```
