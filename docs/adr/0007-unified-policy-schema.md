# ADR-0007: Unified cross-ecosystem policy schema

Status: accepted

Extends ADR-0006 (policy as code). The governance, delivery, `svc-policy`
account, and sub-minute propagation from ADR-0006 are unchanged; this ADR
replaces only the *authoring format* — the three per-format files become one.

## Context

ADR-0006 stores policy as three files in different formats with different, in
fact *inverted*, semantics:

- `npm-rules.yaml` is a **block-list** (default-allow; list the bad) consumed
  in-process by the Verdaccio filter.
- `pypi-constraints.txt` is an **allow-constraint** (`urllib3<2` means *allow
  only* `<2`; `*` means default-deny), enforced by translating to devpi's index
  `constraints` property — our schema never reaches devpi.

So the two dialects disagree on file format, on the underlying question
("list the bad" vs "constrain to the good"), and on where enforcement happens.
ADR-0006 already flagged the cost: "two enforcement dialects must stay
semantically aligned by convention." That convention does not scale. Artea
intends to add formats beyond npm and PyPI (Maven, RPM, Debian, containers — see
ARCHITECTURE.md "Scale-out design"), and a third and fourth bespoke dialect would
compound the drift. The immediate forcing function is automated malicious-package
blocking (OSV.dev): a generator that has to emit two unrelated dialects, and a
human override that has to be expressed two different ways, is fragile.

The hard sub-problem is version ranges. semver, PEP 440, Maven ranges, and
Debian/RPM comparison differ in version *ordering and equality*; no single range
grammar is correct across ecosystems. Any "unification" that tries to flatten the
range grammar is wrong.

## Decision

One schema for all ecosystems, specified in `docs/policy-schema.md`. Key choices:

1. **Unify the envelope, delegate the grammar.** A single `policy.toml` holds a
   flat list of rules, parsed by policy-sync with the stdlib `tomllib` (Python
   3.14) so the service stays stdlib-only. Each rule is tagged with its
   `ecosystem`; its `versions` expression is written in, and interpreted by, that
   ecosystem's native comparator. This is the same typed-range approach OSV uses,
   and it makes OSV ingest a near-identity translation.

2. **One action model with allow-wins precedence.** Each rule is `deny`
   (default) or `allow`. Precedence is the simple **allow-wins** model resolved at
   compile time: an `allow` overrides a `deny` at the granularity the allow names
   — a whole-package allow un-blocks that package entirely (drops its denies), a
   single exact-version allow un-blocks exactly that version. Allow rules support
   only a whole package or a single exact version. `defaults.action` (overridable
   per ecosystem) is the baseline. This gives the human override / OSV
   false-positive escape hatch directly (a curated `allow name ==1.2.3` un-blocks
   one vetted version against a broader `deny`) without a specificity-tier model.

3. **policy-sync becomes the compiler; engines barely change.** policy-sync
   parses and validates `policy.toml`, **resolves precedence at compile time**,
   and emits the effective native artifact each engine already consumes: the
   existing Verdaccio filter YAML shape for npm (an exact-version allow against a
   whole-package deny is emitted as the semver complement `<v || >v`), the devpi
   `root/constrained` constraints for PyPI (a constraint is an allow-list, so a
   deny range is emitted as its complement and multiple denies for one package
   are combined into the single specifier devpi accepts), and the
   `upstream-policy.yaml` (`upstream.min_age`) the Verdaccio CompositePolicyLoader
   reads. Engines see only already-decided blocks; they never evaluate allow/deny.
   This keeps the no-fork rule (R7, ADR-0004) intact and the blast radius small.

4. **Extensibility = adapters.** An ecosystem is an adapter: name normalization,
   namespace semantics (or none), native-range validation, and `is_exact` /
   `exact_value`, plus an emitter. No general range algebra is required. Adding a
   format needs no schema change and no change to other engines.

5. **Whole-policy validation, fail to last-known-good.** A structural error
   (unknown ecosystem, bad target, unparsable range, …) fails the whole sync and
   keeps the previously applied policy in effect, logged loudly via `/healthz`.
   This is distinct from the engines' runtime fail-closed on a missing compiled
   artifact, and from the runtime OSV layer's fail-open behavior for uncached
   lookup failures.

`upstream.min_age` stays a top-level key (it is a registry-wide recency gate, not
a per-package block) and keeps its ADR-0006 behavior.

### Staged scope

Phase 1 compiles all `deny` rules (whole package, npm namespace/scope, native
version ranges), plus `allow` carve-outs at whole-package or single-exact-version
granularity. These are rejected by validation with a clear message: an `allow`
version *range*, an exact-version `allow` against a *version-range* deny
(range-vs-range carving), a whole-package `allow` for a package covered only by a
namespace deny, an npm exact-version allow whose `<v || >v` complement is not a
valid semver range, a PyPI deny range that is not a single complementable
comparator (`<`/`<=`/`>`/`>=`/`==` against a plain version — its allow-list
complement must be one PEP 440 specifier), and `defaults.action = "deny"` for npm
(the filter shape is a block-list; PyPI supports default-deny natively via `*`).

The compiler also rejects, with a clear message, cases where an artifact would be
*syntactically* valid but the engine would still reject it or it would silently
mis-block — these are the failures the round-trip guard now provably catches
because each native validator is a strict subset of the engine's:

- an npm version range with a **non-trailing wildcard** (`1.x.3`, `x.2.3`,
  `>=1.x.3`, `X.2`, `0.x.0`): `semver.validRange` returns `null` for these, so the
  compiler must too (else the filter throws and the whole npm ecosystem fails
  closed). A wildcard may appear only as a trailing segment (`x`, `1.x`, `1.2.x`,
  `1.x.x`);
- a **malformed PyPI package name** (after PEP 503 normalization it must match
  `^[a-z0-9]([a-z0-9-]*[a-z0-9])?$`); a name with a space, `#`, `/`, or a newline
  would emit a constraint devpi's `parse_constraints` 400s, freezing all PyPI;
- a **contradictory combined complement** for one PyPI package (e.g. `deny >=2` +
  `deny <5` → `>=5,<2`): syntactically valid but an empty allow-set that would
  silently block the whole package — rejected so the author uses an explicit
  whole-package deny instead;
- an **unknown/typo'd key** at the `[[rules]]`, `[defaults]`, or
  `[defaults.ecosystems.<eco>]` level (e.g. `verisons = "<2"` instead of
  `versions`): a structural error rather than a silent whole-package deny.

These are enforced by an emit-contract test that feeds representative compiled npm
YAML through the real Verdaccio filter `compilePolicy` and the compiled PyPI
constraints through devpi's `parse_constraints` (or its stdlib contract
reimplementation when devpi's deps are absent), plus semver accept/reject fixture
pairs that pin the npm validator without needing node at CI time.

## Consequences

- One reviewed file, one mental model, one place to express a block or an
  override for every ecosystem; the "aligned by convention" liability of
  ADR-0006 is replaced by a single source compiled deterministically.
- The npm filter plugin and devpi enforcement are unchanged in Phase 1 — they
  keep consuming their current artifacts, now compiler-generated. The work
  concentrates in policy-sync (TOML parser/validator, allow-wins resolver,
  per-ecosystem adapters, emitters).
- The main correctness risk is never emitting a native expression the engine
  would reject (which would fail-closed the whole ecosystem at the engine). The
  npm range validator is a conservative subset of `semver.validRange`; on any
  doubt the compiler rejects at compile time rather than emit. No general range
  algebra is built — the only subtraction is the narrow exact-point npm
  complement, which is unit-tested against the real filter `compilePolicy`.
- Adding an ecosystem becomes a bounded, repeatable task (adapter + emitter), not
  a new bespoke policy dialect.
- Migration is non-breaking: during the transition policy-sync preferred
  `policy.toml` and fell back to the three legacy files when it was absent.
  (The transitional fallback has since been removed — `policy.toml` is now the
  sole authoring input.) Governance, the policy repo, branch protection, and
  propagation are untouched.
- New e2e coverage: the existing S5 (npm version block) and S10 (PyPI constrain)
  must pass authored as unified rules; add a scenario for the specific-`allow`
  override beating a broader `deny`, and one for validation rejecting a malformed
  policy without disturbing last-known-good enforcement.
- A `source` field was introduced as provenance for automated feeds. The runtime
  OSV malicious-package layer uses the same allow-wins override model but queries
  OSV inline instead of compiling a mirrored OSV database into policy artifacts.
</content>
