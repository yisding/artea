# Artea refactoring plan

A whole-codebase review for elegance, maintainability, and size, run as a
fan-out of six parallel subsystem audits (policy-sync, enforcement plugins,
shell layer, gateway, config/Helm, docs/meta). Findings are reconciled here and
grouped by **theme**, because the highest-value work is cross-cutting, not
per-file. Backward compatibility is explicitly **not** a constraint (per the
request); the binding constraints that remain are the `docs/ARCHITECTURE.md`
"Fixed contracts" table, upstream isolation (R7, no forking
Gitea/Verdaccio/devpi), and fail-closed enforcement semantics.

## Headline

The codebase is healthy and unusually well-commented; almost none of the wins
are bug fixes. The size and maintenance cost come from **four structural
duplications**, three of which are kept in sync *by hand* (and at least two have
already silently drifted):

1. The legacy three-file policy format, carried in parallel with the canonical
   `policy.toml` across code, seed files, docs, and e2e scenarios.
2. The compose-vs-Kubernetes config copies (`nginx.conf`, njs, Verdaccio config,
   Gitea settings) — a whole guard script (`check-chart-copies.sh`) exists only
   to police this drift, and it doesn't even cover the riskiest copy.
3. Three independent implementations of the same fail-closed invariants
   (ISO-8601 duration parsing, the age-gate "unknown timestamp ⇒ block" rule,
   the version-range dialect) across TypeScript and two Python services, linked
   only by code comments.
4. Mechanical boilerplate inside the two big files (`e2e/run.sh` 1174 lines,
   `gateway/test/test_routing.py` 722 lines) and the Helm templates.

Estimated net reduction if all themes land: **~2,200–2,600 lines** of
hand-maintained code/docs/config (excluding lockfiles), the deletion of
`scripts/check-chart-copies.sh` and `policy-sync/policy_sync/migrate.py`, and —
more important than line count — the conversion of every "keep in sync by hand"
relationship into a structural single source where drift becomes impossible.

---

## Theme 1 — Delete the legacy three-file policy format end-to-end

`policy.toml` is canonical and wins whenever present. The legacy
`npm-rules.yaml` / `upstream-policy.yaml` / `pypi-constraints.txt` authoring
format survives only as a backward-compat fallback. Dropping it is the single
biggest, lowest-risk reduction available, and it removes the worst over-coupling
in the repo.

| Action | Location | Δ | Effort/Risk |
|---|---|---|---|
| Delete `migrate.py` + its test + `make policy-migrate` | `policy-sync/policy_sync/migrate.py` (383), `tests/test_migrate.py` (235), `Makefile:54-55` | **−618** | S / low |
| Delete the legacy three-file sync fallback; collapse `sync_once` to a single `policy.toml` → compile → emit flow | `policy-sync/policy_sync/sync.py:27-30,67-95,97-167,200-229` (also simplifies `tests/test_sync.py`) | **−110** src | M / med |
| Delete the three legacy seed files; bootstrap seeds only `policy.toml` | `policy/npm-rules.yaml`, `policy/upstream-policy.yaml`, `policy/pypi-constraints.txt`; `scripts/bootstrap.sh:282-284` | **−72** + 3 files | S / low |
| Remove legacy prose duplicated across 8 docs | `docs/policy-schema.md` (§Migration, mapping table), `docs/ARCHITECTURE.md:55,225-229`, `docs/guides/operations.md:164-182`, `README.md:13-15,44-47`, seed-file headers | **−120…160** | M / low |
| Collapse the legacy↔unified e2e scenario clones (S5↔S18, S10↔S19 are near-verbatim, differing only in the policy payload) | `e2e/run.sh:361-374↔937-965`, `441-482↔969-1014` | **−70** | M / med |

`migrate.py` is also the worst offender for coupling you asked me to hunt: it
imports two *private* regexes from `adapters.py` (`_PEP440_COMPLEMENT_OP`,
`_PEP440_SINGLE_CMP_RE`), re-implements a YAML line-parser, and carries its own
TOML string-escaper near-duplicating `compiler.py:_npm_quote`. All of it vanishes.

**Theme total: ~−950 lines + 4 files deleted.** Net effect: one canonical
authoring format, one seed file, and every policy sentence/scenario stops being
written twice.

---

## Theme 2 — Single-source the compose-vs-Kubernetes config (delete the drift guard)

Every gateway/Verdaccio/Gitea config exists as two hand-maintained copies (compose
template + Helm chart). `scripts/check-chart-copies.sh` exists *solely* to detect
the resulting drift — and the most security-sensitive file (the 496-line
`nginx.conf` routing brain) isn't even covered by it. Both nginx copies have
**already drifted** cosmetically (`registry_auth`→`artea_auth`, `@registry_*`→
`@artea_*`, `log_format gateway`→`artea`) — pure divergence that inflates the
diff and makes every routing edit a double-edit hazard.

A conceptual diff shows the two `nginx.conf` files differ in **only** two real
ways: the upstream-resolution block (compose `resolver` + `set $x_upstream`
vs k8s `upstream {}` blocks, ~8 lines) and the `__ARTEA_NAMESPACE__` token.
The two Verdaccio configs differ in **only** two lines (gitea host; policy
file-paths vs URLs). The njs modules and Gitea templates are **byte-identical**
copies.

| Action | Location | Δ | Effort/Risk |
|---|---|---|---|
| Make `nginx.conf` a single templated source; emit the upstream block via an `{{ if .upstreamMode }}` switch; render for both targets | `gateway/nginx.conf.template` ↔ `deploy/helm/artea/files/gateway/nginx.conf` | **−480** | M / med |
| Replace the 4 byte-identical chart copies (2 njs + 2 gitea tmpl) with symlinks (or generate them); **delete `check-chart-copies.sh`** + its Makefile target + README "keep in sync" table | `deploy/helm/artea/files/gateway/{pep503,pep700}.js`, `files/gitea-templates/*`, `scripts/check-chart-copies.sh` | **−280** + 1 script | S / low |
| Lift the Verdaccio config out of `values.yaml` into one `files/verdaccio/config.yaml` rendered via `.Files.Get \| tpl`; single source with compose | `verdaccio/config.yaml.template` ↔ `deploy/helm/artea/values.yaml:332-414` | **−80** | M / low |
| Extract the ~25 shared Gitea *policy* keys (registration/units/packages/footer) into one neutral source generating both INI and YAML fragments | `gitea/app.ini.template` ↔ `values.yaml:200-261` | ~−20 | L / med |
| Add `artea.service` + `artea.httpProbe` helpers; collapse the 3 near-identical Service/probe blocks | `deploy/helm/artea/templates/{devpi,policy-sync,gateway}.yaml`, `_helpers.tpl` | ~−30 | S / low |
| Single-source version pins (today mirrored in `.env` and `values.yaml` with "keep in sync" comments) | `.env.example`, `values.yaml`, `values-local.yaml` | ~−6 | S / low |

**Mechanism that makes drift impossible:** every config has exactly one on-disk
source; both worlds consume that source (the chart already does
`tpl (.Files.Get "files/gateway/nginx.conf")` for nginx — point `.Files.Get` at
a symlink to the single source, or generate `files/` in the same render step and
gitignore it). Once there is no second file, `check-chart-copies.sh` has nothing
to check and is deleted.

**Theme total: ~−850 lines + a whole guard apparatus gone.**

**Status (landed):** the nginx single source (one Helm template with a
`gateway.upstreamMode` switch; compose rendered through Helm via
`scripts/render-nginx.sh`), the four byte-identical chart copies → symlinks with
`check-chart-copies.sh` and its Makefile/CI/README hooks deleted, the Verdaccio
config single source (`files/verdaccio/config.yaml` delivered through the
subchart's `existingConfigMap`), and the Service/probe helpers all landed.
**Deferred — the shared Gitea policy keys and the cross-system version pins.**
Both because the Kubernetes side has no clean hook to consume one generated
source: the Gitea subchart takes a *static* `values.yaml gitea.config` (no
`existingConfigMap`-style runtime hook, and replacing the generated `app.ini`
breaks the chart's DB/session/secret auto-wiring), and the compose version pins
live in the `.env` that `docker compose` auto-loads directly across the
Makefile, `bootstrap.sh`, `e2e/run.sh`, and `smoke.sh`. Single-sourcing either
would mean committing a generated file plus a new CI regen-guard (trading one
guard for another, against this theme's intent) or broad `--env-file` churn, so
they remain documented duplications.

---

## Theme 3 — Single-source the cross-language fail-closed invariants

The TS filter, the Python devpi plugin, and the Python policy-sync compiler each
re-encode the **same three policy primitives**, kept aligned only by hand-written
comments (`policy_model.py:26` literally says it "ports the Verdaccio filter's
ISO_DURATION_RE… so policy-sync accepts exactly the same strings"). These are the
invariants whose silent drift would break fail-closed enforcement — the most
dangerous duplication in the system even though it's small in line count.

| Primitive | TS site | devpi-Py site | policy-sync-Py site |
|---|---|---|---|
| ISO-8601 `min_age` parse | `verdaccio-filter-artea/src/policy.ts:28-60` | `artea_devpi_policy/.../main.py:40-88` | `policy_model.py:26-96` |
| Age-gate "unknown ⇒ block" | `index.ts:78-86` | `main.py:311-317,371` | n/a |
| Version-range dialect | `policy.ts:118,142-145` | `main.py:91-110,319-325` | `adapters.py:8-12,41-75` |

**Actions:**
- Add language-agnostic test-vector files under `docs/policy-spec/`:
  `min-age-vectors.json` (string → seconds\|error), `age-gate-vectors.json`
  (`(minAge, publishedAt\|null, now)` → allow\|block),
  `version-range-vectors.json` (range+version → matches?, range → valid?). Each
  implementation loads them in its unit tests → lockstep becomes CI-enforced
  instead of comment-enforced.
- Within Python, merge the two duplicate ISO-8601 parsers (devpi `main.py` and
  policy-sync `policy_model.py`) into one shared helper. (~−30 net)

**Theme total: ~LOC-neutral (+vectors, −Python dup), maximum correctness payoff.**

---

## Theme 4 — Intra-file decomposition, dedup, and dead code (elegance)

### Shell layer (`e2e/run.sh` 1174, `scripts/bootstrap.sh` 524)
The shape is right (named scenarios + a harness + `lib.sh`); don't rewrite in
Python (it must drive real `npm`/`pip`/`twine`/`git`). Push helpers down:
- Add `assert_eq` / `assert_code` / `assert_contains` / `assert_origin` /
  `gw_get` / `packument_has` to `lib.sh`. The bare `|| { echo…; return 1; }`
  assertion is hand-spelled **121×** in `run.sh`. **~−110** (run.sh) / **+50** (lib.sh).
- `bootstrap.sh`: 12 inline `python3 -c` JSON snippets → one `json_get`; 3
  divergent retry loops → one `retry_until` (the canonical `wait_for` already
  exists in `lib.sh` but bootstrap can't see it). **~−45**.
- `smoke.sh` reimplements `http_code`, credential preamble, and the policy-sync
  `/healthz` probe that now lives in 3 files — `source lib.sh` instead. **~−12**.
- Net (with clones from Theme 1): `run.sh` 1174 → **~780**, `bootstrap.sh` 524 → **~470**.

### Gateway test (`test_routing.py` 722)
Parametrize the repeated "auth matrix" assertions (anonymous→401, nonmember→403,
no-package-scope→403, good→200) repeated across npm/pypi/gitea-package locations,
and the encoded-rejection tests; add a `no_new_requests()` context manager for the
copy-pasted request-count bookkeeping. **~−180…250**, coverage preserved.

### Verdaccio filter (`policy.ts` 453)
Split into `policy-compile.ts` (duration + compile + `isNameBlocked`/
`isVersionBlocked`) and `policy-loaders.ts` (File/Http/Composite + factory);
extract the duplicated success-log line and a `Transition` "log-once" helper
shared by both loaders (both modes stay — compose=file, k8s=HTTP are load-bearing);
remove the `minAgeMs`-on-`CompiledPolicy` footgun (only the upstream source should
own age). **~−50…70**.

### devpi plugin (`main.py` 414)
The `constrain_all`/`min_age`/`include_legacy` preamble is repeated across all
**4** filter methods (`:240,262,286,320`); extract `_constraint_decision()` + one
generic `_filter_iter()`. **~−30…45**.

### policy-sync compiler
Move the 51 lines of PEP-440 version-tuple math out of `compiler.py:244-294`
onto `PypiAdapter` (where the ADR says dialect logic belongs); decompose the
116-line `_emit_pypi` (`compiler.py:297-412`) into per-pass emitters that
validate at construction instead of the trailing char-scan guard; consider
relocating the 589-line `enrich.py` (PEP-700 enrichment — shares no code with the
compiler) into its own subpackage for boundary clarity.

### Dead code (pure deletions, all low-risk)
- `policy-sync/policy_sync/gitea.py:33-74` — `fetch_text`/`fetch_json` have zero
  callers. **−42**.
- `@types/jsonwebtoken` devDependency in both plugins — neither imports
  `jsonwebtoken`. Remove.
- `verdaccio-filter-artea/src/policy.ts:19` `emptyPolicy` exported but only used
  internally; numeric branch of `parseDurationMs` is dead for the YAML config shape.
- `.gitignore:13,15` — duplicate `.generated/` entry.

**Theme total: ~−450…600 lines.**

---

## Theme 5 — Build/CI consolidation

- **Dockerfiles:** `devpi/`, `policy-sync/`, `scripts/Dockerfile.bootstrap`
  share a byte-identical `FROM python:3.14-slim@sha256:…` digest, the same
  4-line ADR-0004 comment, and the same non-root scaffold. A shared `artea-base`
  stage collapses **3 digest pins → 1** (assess against the per-image build
  contexts first). ~−15…25.
- **CI:** `images.yml` and `kind-e2e.yml` both encode the same image→context
  mapping; kind-e2e unrolls it as 4 copy-pasted `build-push-action` steps.
  Convert to a `strategy.matrix` (or a shared composite action) keyed on the same
  `{name, context, dockerfile}` list. ~−40…60.
- Reconcile the scenario count: workflows/guides say S1–S17/S18 while
  `ARCHITECTURE.md` defines S1–S21. State it once.

**Theme total: ~−60…90 lines + 2 digest pins consolidated.**

---

## Suggested sequencing

1. **Theme 1** (legacy removal) — biggest, lowest-risk, unblocks the e2e clone
   collapse and half the doc cleanup. Pure deletion.
2. **Theme 2** (config single-source) — eliminates the highest-risk *unguarded*
   drift (nginx.conf) and deletes the guard apparatus. Do the cosmetic rename +
   `check-chart-copies` retirement first (free), then the nginx unification.
3. **Theme 3** (cross-language vectors) — small, high correctness payoff; do
   before any change to duration/age-gate/range logic.
4. **Themes 4 & 5** — incremental elegance/dedup; safe to do piecemeal behind the
   existing test suites (`test_routing.py`, `filter.test.ts`, `http.test.ts`,
   `auth.test.ts`, `test_artea_devpi_policy.py`, the S1–S21 e2e suite).

Each change is covered by an existing test suite or is a pure deletion; the
fail-closed and dependency-confusion guarantees in `ARCHITECTURE.md` are
preserved because no routing/enforcement *logic* moves — only its duplication.
