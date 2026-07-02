# Unified policy schema (v1)

This is the canonical specification for Artea's cross-ecosystem block-list
syntax. It replaces the three per-format policy files described in
[ADR-0006](adr/0006-policy-as-code.md) (`npm-rules.yaml`, `pypi-constraints.txt`,
`upstream-policy.yaml`) with one schema that every present and future package
format shares. The governance, delivery, and propagation model from ADR-0006 is
unchanged; only the authoring format changes. The design rationale lives in
[ADR-0007](adr/0007-unified-policy-schema.md).

## What is and is not unified

**Unified:** the file, the rule structure, the allow/deny action model, the
precedence semantics, the namespace/scope concept, and rule metadata.

**Deliberately not unified — the version-range grammar.** Version *ordering and
equality* are ecosystem-specific (semver `1.0.0-rc1`, PEP 440 `1.0.0rc1`, Maven
`[1.0,2.0)`, Debian `~`). No single grammar is correct across ecosystems, so a
rule's `versions` expression is written in, and interpreted by, the comparator of
the rule's own `ecosystem`. This mirrors how OSV models affected ranges (typed
per ecosystem) and is the one thing this schema does not try to flatten.

## File

A single file in the policy repo, `${ARTEA_NAMESPACE}/registry-policy`:

```
policy.toml
```

The source policy is **TOML**, parsed by `policy-sync` with the stdlib `tomllib`
(Python 3.14) — no new dependency, the service stays stdlib-only. The surface
syntax is TOML; the per-engine artifacts the compiler *emits* keep their existing
formats (YAML for the npm filter, a constraints text for devpi). Source format
need not equal emit format.

## Top-level structure

```toml
schema = 1                 # required; integer; gates forward-compatible changes

[defaults]
action = "allow"           # registry baseline: allow (block-list) | deny (allow-list)

[upstream]
min_age = "P3D"            # ISO 8601 duration; hide upstream versions younger than this. OMIT → P0D (gate disabled)

[osv]
malicious_packages = true  # query OSV.dev inline for OpenSSF MAL-* records

[[rules]]                  # ordered array of tables; order is NOT significant
ecosystem = "…"
# …
```

`defaults.action` is the verdict when no rule matches a queried package. `allow`
is the normal pull-through registry (default-allow, list the bad). `deny` is a
locked-down registry (default-deny, only listed packages pass) and may be set
per ecosystem via a `defaults` override (below) — useful where one format is
locked down while another stays open.

Per-ecosystem default override:

```toml
[defaults]
action = "allow"

[defaults.ecosystems.pypi]
action = "deny"            # PyPI locked down; everything else default-allow
```

## Rule fields

| Field | Required | Type | Meaning |
|-------|----------|------|---------|
| `ecosystem` | yes | string | Target ecosystem id (`npm`, `pypi`, …). Must have a registered adapter. |
| `name` | one of `name`/`namespace` | string | Full package name in the ecosystem's canonical form. Mutually exclusive with `namespace`. |
| `namespace` | one of `name`/`namespace` | string | Scope/group; matches every package under it. Only valid for ecosystems whose adapter supports namespaces. |
| `versions` | no | string | Version expression in the **ecosystem's native dialect**. Omitted = all versions. Only valid with `name`. |
| `action` | no | `deny` \| `allow` | Defaults to `deny`. `deny` blocks; `allow` is an explicit carve-out (see Precedence). |
| `reason` | no | string | Free text for humans; surfaced in logs and 403 bodies where practical. |
| `source` | no | string | Provenance. `curated` is the default for human-authored rules; `osv` is reserved for OSV-derived policy metadata. Curated compiled policy fails closed, while the runtime OSV layer fails open for uncached lookup failures. |
| `expires` | no | string (RFC 3339) | Optional. A rule whose `expires` is in the past is ignored. Resolved at compile time, so expiry takes effect within one sync/poll interval (≤ `POLICY_SYNC_POLL_SECONDS`). |

## Ecosystems and their dialects

Each ecosystem is a registered adapter declaring its name normalization,
namespace support, and version comparator. v1 ships two; adding a format is
adding an adapter (see Extensibility).

| Ecosystem | Name normalization | Namespace | Version dialect |
|-----------|--------------------|-----------|-----------------|
| `npm` | as the registry canonicalizes (lowercase); scoped names include the `@scope/` prefix | yes — `@scope` (leading `@` optional in a rule; normalized to include it) | npm semver ranges (`<4.17.21`, `>=1 <2`, `1.2.x`, `<2 \|\| >3`) |
| `pypi` | PEP 503 (lowercase, collapse runs of `-_.` to a single `-`) | no — a `namespace` rule for `pypi` is a validation error | PEP 440 specifiers (`<2`, `==1.2.3`, `>=5.4,<7`, `!=1.2.3`) |

## Precedence and evaluation (allow-wins)

Precedence is the simple **allow-wins** model, resolved at **compile time** so
each enforcement engine only ever sees already-decided blocks:

- An `allow` rule wins over a `deny` rule **at the granularity the allow names**.
- A **whole-package allow** (`name`, no `versions`) un-blocks that package
  entirely: every deny for that exact package is dropped.
- A **single exact-version allow** (`name` + a `versions` expression that denotes
  exactly one version, e.g. npm `1.2.3`, PyPI `==1.2.3`) un-blocks exactly that
  version.
- Allow rules support **only** those two shapes — a whole package, or a single
  exact version. An allow with a version *range* is a validation error.
- If no allow applies, the deny rules stand. If nothing matches at all, the
  verdict is the ecosystem's `defaults.action`.

This is the human override / false-positive escape hatch: a curated
`allow name ==1.2.3` un-blocks one vetted version against a broader `deny`, and a
whole-package `allow` un-blocks a package an automated rule flagged. There is no
specificity-tier ranking and no "deny beats allow" — at the granularity an allow
names, the allow wins.

Carving a single package out of a **namespace** deny is not expressible in the
npm block-list and is a validation error (allow the package by not denying its
scope, or deny the individual packages instead).

## Worked examples

Every capability of the three superseded files is expressible, and more.

```toml
schema = 1

[defaults]
action = "allow"

[upstream]
min_age = "P3D"            # hide upstream versions younger than this; omit → P0D (gate disabled)

# whole-package block (old npm `packages: [event-stream]`)
[[rules]]
ecosystem = "npm"
name = "event-stream"
action = "deny"
reason = "malicious: bitcoin-stealer payload"
source = "curated"

# scope block (old npm `scopes: ["@evil-corp"]`)
[[rules]]
ecosystem = "npm"
namespace = "@evil-corp"
action = "deny"

# version-range block (old npm `{name: left-pad, versions: "<1.0.0"}`)
[[rules]]
ecosystem = "npm"
name = "left-pad"
versions = "<1.0.0"
action = "deny"

# PyPI "allow only <2" (old `urllib3<2`) == deny the complement
[[rules]]
ecosystem = "pypi"
name = "urllib3"
versions = ">=2"
action = "deny"
reason = "pin to 1.x for compatibility"

# PyPI kill a whole package (old `pkg==0`)
[[rules]]
ecosystem = "pypi"
name = "some-pkg"
action = "deny"

# human override: allow one vetted version against a broader deny (allow-wins)
[[rules]]
ecosystem = "pypi"
name = "internal-fork-of-flagged-pkg"
versions = "==1.4.2"
action = "allow"
source = "curated"
reason = "vendored, reviewed internally"
```

Mapping from the old files:

| Old | Unified rule |
|-----|--------------|
| npm `packages: [name]` | `ecosystem = "npm"`, `name`, `action = "deny"` |
| npm `packages: [{name, versions}]` | same, with `versions` |
| npm `scopes: [s]` | `ecosystem = "npm"`, `namespace = s`, `action = "deny"` |
| pypi `name<2` (constrain) | `ecosystem = "pypi"`, `name`, `versions = ">=2"`, `action = "deny"` |
| pypi `name==0` (kill) | `ecosystem = "pypi"`, `name`, `action = "deny"` |
| pypi `*` (default-deny) | `[defaults.ecosystems.pypi]` `action = "deny"` |
| `upstream.min_age` | unchanged — see below |

A devpi constraint is an **allow-list** (versions matching the specifier pass;
the rest are blocked). The author always writes intent as a `deny`; the compiler
emits the **complement** as the devpi constraint. So `deny pypi name >=2`
compiles to the constraint `name<2` (allow only `<2` == block `2.x`). Each
deny range must be a single complementable comparator (`<`, `<=`, `>`, `>=`,
`==`) against a plain version; a compound specifier set, `~=`, `===`, or a
`==X.*` wildcard is **rejected** because its complement is not one PEP 440
specifier.

A "constrain to allowed set" with gaps becomes a union of denies, and the
compiler combines them into the **single** devpi constraint devpi accepts (a
repeated project name is rejected by the engine). Allow only `[5.4, 7)` of
`pyyaml`:

```toml
[[rules]]
ecosystem = "pypi"
name = "pyyaml"
versions = "<5.4"
action = "deny"

[[rules]]
ecosystem = "pypi"
name = "pyyaml"
versions = ">=7"
action = "deny"
```

The two denies compile to the complements `>=5.4` and `<7`, intersected into the
single constraint `pyyaml>=5.4,<7`.

### Upstream age gate

`upstream.min_age` from ADR-0006 is already cross-format and stays a top-level
key rather than a per-rule field, because it is a registry-wide recency gate, not
a per-package block:

```toml
[upstream]
min_age = "P3D"            # ISO 8601 duration; P0D disables
```

### OSV malicious package layer

`[osv] malicious_packages = true` enables request-time filtering for OSV.dev /
OpenSSF malicious-package records. Verdaccio and devpi ask policy-sync about the
actual public package versions they are about to list or serve; policy-sync calls
OSV's `querybatch` API and caches bounded per-version verdicts. Artea does not
mirror the OSV database.

Only OSV IDs with the `MAL-` prefix block a version. Ordinary vulnerability
advisories such as CVE, GHSA, and PYSEC records do not affect registry
availability. A curated `allow` rule for the whole package or exact version
overrides an OSV malicious hit for false-positive handling.

The OSV layer is disabled when the `[osv]` table is absent or
`malicious_packages = false`. If OSV or policy-sync's internal OSV endpoint is
unavailable, enforcement fails open for uncached versions while continuing to
block any still-fresh cached malicious verdicts. This is intentionally separate
from the compiled curated policy artifacts, which continue to fail closed when
missing or unreadable.

## Validation

The policy is validated as a whole before anything is applied:

- `schema` must be a known version; unknown `ecosystem` (no adapter), a rule
  with both or neither of `name`/`namespace`, a `namespace` rule for an
  ecosystem without namespace support, an `action` outside `{allow, deny}`, or a
  `versions` string that does not parse in the ecosystem's dialect — all are
  **structural errors**.
- An **unknown/typo'd key** at the `[[rules]]`, `[defaults]`, or
  `[defaults.ecosystems.<eco>]` level is a **structural error** (top-level keys
  are already validated). This catches a silent footgun: `verisons = "<2"`
  (a typo for `versions`) would otherwise be ignored and the rule would become a
  whole-package deny instead of a version-range deny.
- A **PyPI package name** must, after PEP 503 normalization (runs of `-_.`
  collapse to a single `-`, lowercased), match `^[a-z0-9]([a-z0-9-]*[a-z0-9])?$`.
  A name with a space, `#`, `/`, or a newline is a **structural error** — it would
  otherwise emit a constraint devpi's `parse_constraints` rejects (HTTP 400),
  failing the whole PyPI freeze.
- A structural error fails the whole sync: the previously applied policy stays in
  effect (last-known-good), the failure is logged loudly, and `/healthz` reports
  `last_sync_ok: false`. A broken authoring change never tears down enforcement.
- This is distinct from the runtime fail-closed behavior of the enforcement
  plugins (a missing/unreadable *compiled* artifact still fails closed there) and
  from the runtime OSV malicious-package layer's fail-open behavior for uncached
  lookup failures.

## Compilation and enforcement

`policy-sync` is the compiler. It parses `policy.toml` once, **resolves
precedence at compile time** (allow-wins), and emits the effective, native
artifact each enforcement engine already consumes — the engines do not evaluate
allow/deny themselves:

- **npm** → the effective deny set in the existing Verdaccio filter YAML shape
  (blocked names, scopes, and semver ranges). A whole-package allow drops that
  package's denies; an exact-version allow against a whole-package deny is emitted
  as the semver complement `<v || >v` (the filter ORs ranges, so the union is
  native), when that complement is a valid semver range — otherwise the
  combination is rejected. The filter plugin is unchanged.
- **pypi** → the effective constraints PATCHed onto the devpi `root/constrained`
  index, where a constraint is an **allow-list**. A deny range is emitted as its
  **complement** (`deny >=2` → `name<2`); multiple denies for one package are
  combined into the single comma-joined specifier devpi accepts (`deny <5.4` +
  `deny >=7` → `name>=5.4,<7`). A whole-package deny → `name==0` (default-allow)
  or covered by the trailing `*` (default-deny); an exact-version allow against a
  whole-package deny → `name==v`; default-deny → `*`. Exactly the shape devpi
  consumes today.
- **upstream** → the `upstream-policy.yaml` artifact (`upstream.min_age`) the
  Verdaccio `CompositePolicyLoader` reads. The loader takes `min_age` solely from
  this file, so the compiler emits it alongside the npm rules; the same value
  also feeds the devpi `min_upstream_age`.
- **OSV malicious packages** → no compiled block-list artifact. policy-sync keeps
  the last-known-good parsed `policy.toml` in memory and serves
  `POST /osv/querybatch` for Verdaccio and devpi to query inline as package
  requests arrive. Runtime OSV verdicts are cached with positive/negative TTLs;
  deployments can persist that verdict cache across policy-sync restarts without
  changing the policy semantics.

Because precedence is resolved in the compiler, each engine only ever sees
already-decided blocks. Beyond parse/normalize/validate, the per-ecosystem
adapter needs only **`is_exact(expr)`** / **`exact_value(expr)`** (to classify an
exact-version allow and build the npm point complement). **No general range
algebra is implemented** — there is no `range_contains` and no range-vs-range
subtraction; the only "subtraction" is the narrow exact-point npm complement.

### Staged capability

Phase 1 (initial) supports: all `deny` rules (whole package, namespace/scope for
npm, and version ranges in the native dialect), plus `allow` carve-outs at
whole-package or single-exact-version granularity (the malware-block and OSV
false-positive-override cases). The following are **rejected by validation with a
clear, actionable message**:

- an `allow` with a version *range* (only a whole package or a single exact
  version is supported);
- an exact-version `allow` against a *version-range* deny (range-vs-range
  carving);
- a whole-package `allow` for a package covered only by a `namespace` deny
  (the npm block-list cannot express "scope minus one name");
- an exact-version `allow` whose npm complement `<v || >v` is not a valid semver
  range;
- a `pypi` deny range that is not a single complementable comparator (`<`, `<=`,
  `>`, `>=`, `==`) against a plain version — a compound specifier set, `~=`,
  `===`, or a `==X.*` wildcard — because devpi reads a constraint as an
  allow-list and the deny's complement must be one PEP 440 specifier;
- an `npm` version range with a **non-trailing wildcard** (`1.x.3`, `x.2.3`,
  `>=1.x.3`, `X.2`, `0.x.0`): `semver.validRange` rejects these, so the compiler
  rejects them too rather than emit a range the filter would throw on (which would
  fail-close the whole npm ecosystem). A wildcard may appear **only as a trailing
  segment** (`x`, `1.x`, `1.2.x`, `1.x.x`); the npm range validator is a strict
  subset of `semver.validRange`, so anything it accepts the filter accepts —
  making the compiler's round-trip guard trustworthy;
- two or more `pypi` denies for one package whose complements **combine into an
  empty allow-set** (`deny >=2` + `deny <5` → `>=5,<2`): syntactically a valid
  PEP 440 set but it would silently block the whole package — use an explicit
  whole-package deny instead;
- `defaults.action = "deny"` for `npm` (the Verdaccio filter shape is a
  block-list and cannot express default-deny; `pypi` supports it natively via
  `*`).

## Extensibility

Adding an ecosystem (Maven, OCI, Debian, …) is adding an **adapter** and
requires **no schema change** and **no change to other engines**:

1. Register the ecosystem id.
2. Implement the comparator: name normalization, namespace semantics (or declare
   none), native-range validation, and `is_exact` / `exact_value` (used for the
   exact-version allow escape hatch).
3. Implement the emitter: produce whatever that engine enforces with (a native
   constraint file, an index property, an in-process rule set, …), or have the
   engine consume the compiled per-ecosystem view directly.

This slots a "policy adapter" step into the architecture's existing new-format
recipe (`docs/ARCHITECTURE.md`, "Scale-out design").
