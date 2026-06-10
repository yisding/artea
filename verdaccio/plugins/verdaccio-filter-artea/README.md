# verdaccio-filter-artea

Verdaccio metadata filter plugin (`IPluginStorageFilter`, `filter_metadata` hook) that
enforces Artea's npm block policy (requirement R3). It rewrites packuments before
Verdaccio serves them: blocked versions disappear from `versions`/`time`, dist-tags
pointing at removed versions are dropped (`latest` is re-pointed to the highest
remaining version), and fully-blocked names are served with zero versions so installs
fail with the standard "No matching version found" npm error.

## Configuration (verdaccio config.yaml)

```yaml
filters:
  filter-artea:
    policy_file: /policy/npm-rules.yaml   # default; the shared policy-data volume
```

## Policy file schema (`npm-rules.yaml`)

The file lives in the Gitea repo `artea/registry-policy` and is written into the
`/policy` volume by policy-sync. Top-level key `blocked` with two optional lists:

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

The plugin `stat()`s the policy file on every `filter_metadata` call and re-parses it
only when the mtime changes — no restart needed after policy-sync writes a new file.
If a new revision fails to parse (invalid YAML or wrong structure), the **last good
policy stays in effect** and an error is logged; the file is not re-parsed until its
mtime changes again.

## Fail-open on missing file (deliberate)

If the policy file does not exist, the plugin behaves as if the policy were empty and
nothing is blocked. This is intentional: the policy file is provisioned asynchronously
(policy-sync fetches it from Gitea after bootstrap), and a fresh stack must be able to
serve packages before the first sync lands. Blocking is a curation feature, not an
access-control boundary — access control is the auth plugin's job, and the private
`@artea` scope is denied in `config.yaml` package rules, not here. If the file
disappears after having existed, the plugin reverts to the empty policy and logs a
warning.

## Develop

```sh
pnpm install        # from verdaccio/plugins/
pnpm build          # tsc -> dist/ (CommonJS, what verdaccio 6 loads)
pnpm test           # vitest
```
