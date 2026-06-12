# ADR-0006: Policy as code in a reviewed Gitea repo

Status: accepted (v1)

## Context

R3 requires blocking public packages and specific versions from pull-through.
Block decisions are security-sensitive: they need review, audit history, and
rollback. The enforcement points are heterogeneous — a Verdaccio filter plugin
for npm and a devpi `root/constrained` index for PyPI — and must not drift
from each other or from what was approved.

Alternatives: admin UI toggles in each cache (no review, no audit, two places
to edit, lost on cache wipe — caches are disposable per ADR-0003); config
files baked into the deployment repo (every block requires a redeploy).

## Decision

Policy lives in the Gitea repository `${ARTEA_NAMESPACE}/registry-policy` as
three files:

- `npm-rules.yaml` — blocked npm names, scopes, and semver ranges, consumed by our
  Verdaccio filter plugin (re-read from `/policy` on mtime change, no restart).
- `upstream-policy.yaml` — shared public-upstream policy. v1 defines
  `upstream.min_age` as an ISO 8601 duration such as `P3D` or `PT72H`; npm,
  PyPI, and future artifact types must consume this same value.
- `pypi-constraints.txt` — devpi-constrained format (`name<2`,
  `name ==1.2.3`, `*` default-deny). The constraints are applied to the
  `root/constrained` index, alongside `min_upstream_age` from
  `upstream-policy.yaml`.

The `policy-sync` service turns repo state into enforcement: it receives the
repo's push webhook (plus a startup sync and a slow poll as fallback), fetches
all three files via Gitea's raw-content API, writes them to the shared
`policy-data` volume in compose, serves npm and upstream policy over HTTP to
Verdaccio in Kubernetes, and pushes the PyPI constraints plus
`min_upstream_age` into devpi. It authenticates as `svc-policy`, a
dedicated non-admin service account whose only access is read-only on the
policy repo (via the `policy-readers` team) with a PAT scoped to
`read:repository` — a leaked policy-sync credential cannot write anything.

Changes therefore go through ordinary Gitea pull requests on a repo that
exists anyway (Gitea is the control plane, ADR-0001): review via approvals,
audit via git history, rollback via revert. This is enforced, not just
convention: the repo's default branch carries branch protection (direct pushes
blocked for everyone except the configured admin allowlist, ≥1 required
approval, rejected reviews block the merge), and developers sit in a
`developers` team (code/pulls/packages write, no admin) rather than in org
Owners — so no developer credential can bypass the PR path (e2e scenario S14).

## Consequences

- Sub-minute propagation after merge, with no service restarts.
- The git history *is* the audit log of what was blocked, when, and by whom.
- Policy survives cache wipes and restores trivially (it is in the Gitea
  backup).
- Two enforcement dialects must stay semantically aligned by convention; e2e
  scenarios S5 and S10 guard the version-policy wiring, while unit tests cover
  the shared age-gate parsing and hot-path enforcement in Verdaccio and devpi.
- policy-sync needs the `svc-policy` account, its PAT, and webhook plumbing —
  bootstrap steps (scenario S1); the admin allowlist on the protected branch
  also keeps the e2e suite's direct policy edits (as the configured admin)
  working.
- New formats add a policy file section plus a policy-sync adapter, keeping
  the same review workflow.
