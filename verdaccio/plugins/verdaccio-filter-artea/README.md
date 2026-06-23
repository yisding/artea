# verdaccio-filter-artea

One package, two Verdaccio plugin roles, enforcing Artea's npm block policy and
the shared upstream age policy
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
  versions younger than the configured upstream age. When `osv_url` is set, both
  metadata and tarball paths also reject versions policy-sync reports as OSV
  malicious.
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
    policy_file: /policy/npm-rules.yaml   # file mode (test/debug); K8s uses policy_url
    upstream_policy_file: /policy/upstream-policy.yaml
    osv_url: http://policy-sync:8920/osv/querybatch
    osv_timeout_ms: 5000
    npm_registry_url: https://registry.npmjs.org

middlewares:
  filter-artea:                           # same package, middleware role (S13)
    policy_file: /policy/npm-rules.yaml
    upstream_policy_file: /policy/upstream-policy.yaml
    osv_url: http://policy-sync:8920/osv/querybatch
    osv_timeout_ms: 5000
    npm_registry_url: https://registry.npmjs.org
```

In Kubernetes, use `policy_url` and `upstream_policy_url` instead. `osv_url`
always points at policy-sync's internal `POST /osv/querybatch` endpoint.

## Policy file schema

The file lives in the configured Gitea policy repo
`${ARTEA_NAMESPACE}/registry-policy` (or explicit `POLICY_REPO`) and is served
over HTTP by policy-sync (or written to `/policy` in file mode).

`npm-rules.yaml` owns npm-specific blocks:

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

`upstream-policy.yaml` owns the cross-format public upstream recency gate:

```yaml
upstream:
  # ISO 8601 duration. P3D = three days; P0D disables the age gate.
  min_age: P3D
```

Semantics:

- Multiple `packages` entries for the same name are OR-ed together.
- `upstream.min_age` accepts ISO 8601 week/day/time durations (`P3D`, `PT72H`,
  `P1DT12H`). Month/year units are intentionally unsupported because their
  duration depends on a calendar. When the gate is active, a version without a
  parseable `time[version]` publish timestamp is treated as blocked.
- Range matching uses `{ includePrerelease: true, loose: true }`, so `<2` also blocks
  `2.0.0-beta`-style prereleases of blocked ranges — a blocklist should over-block.
- Invalid `versions` semver ranges fail the whole policy load so the registry fails
  closed instead of silently weakening the block policy.
- Any other malformed entry (missing `name`, non-string scope, non-string
  `versions`) likewise fails the whole policy load, so the registry fails closed
  rather than silently dropping rules.
- An empty file, or a file without `blocked`, is an empty policy.
- OSV checks are request-time, not part of `npm-rules.yaml`: policy-sync decides
  whether `[osv] malicious_packages` is enabled and blocks only OSV `MAL-*`
  records. Curated `allow` rules in `policy.toml` override OSV false positives.

## Reload behavior

The plugin `stat()`s the policy file on every request and re-parses it only when the
mtime changes — no restart needed after policy-sync writes a new file. A
stale-but-valid file keeps serving as last-known-good: freshness is policy-sync's
job, not the plugin's.

## Failure mode: fail-closed (default)

If a configured policy file is **missing or unparsable** (invalid YAML or wrong
structure), the plugin rejects public-package traffic instead of silently
serving everything unfiltered (e2e scenario S15):

- the middleware answers tarball requests with
  `503 {"error": "policy unavailable: ..."}`;
- the filter strips every version from every packument it sees, so installs fail.
  (It cannot 503: Verdaccio swallows errors thrown by filter plugins and would serve
  the packument unfiltered — stripping is the only reliable rejection.)

When rejection kicks in differs per mode:

- **File mode**: immediately, whenever the file is **missing or unparsable**
  (invalid YAML, wrong structure, or invalid semver ranges). The failed state clears
  automatically through the same mtime/stat reload: as soon as the file reappears or
  is fixed, the policy applies again — no restart.
- **URL mode**: transient failures are normal on a network, so the last
  successfully fetched policy keeps serving (last-known-good, in memory) while
  polls fail — whether the failure is a connection error, a non-2xx response, or an
  unparsable body. Only when failures persist past `fail_grace_ms` (default 60s)
  does the plugin fail closed — plus on **cold start**, when nothing has ever been
  fetched (there is no known-good policy to serve). The first successful poll
  recovers automatically.
- **OSV mode**: `osv_url` lookup failures fail open for that OSV-only decision.
  The compiled policy file and upstream age gate above still keep their normal
  fail-closed behavior.

Load failures and the open/closed transitions are logged once per transition
(`warn`/`error` level); rejected requests log at `warn`.

## Develop

```sh
pnpm install        # from verdaccio/plugins/
pnpm build          # tsc -> dist/ (CommonJS, what verdaccio 6 loads)
pnpm test           # vitest
```
