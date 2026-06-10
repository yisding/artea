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

Policy lives in the Gitea repository `artea/registry-policy` as two files:

- `npm-rules.yaml` — blocked npm names, scopes, and semver ranges, consumed by
  our Verdaccio filter plugin (re-read from `/policy` on mtime change, no
  restart).
- `pypi-constraints.txt` — devpi-constrained format (`name<2`,
  `name ==1.2.3`, `*` default-deny), applied to the `root/constrained` index.

The `policy-sync` service turns repo state into enforcement: it receives the
repo's push webhook (plus a startup sync and a slow poll as fallback), fetches
both files via Gitea's raw-content API using a service PAT, writes
`npm-rules.yaml` to the shared `policy-data` volume, and pushes the
constraints into devpi.

Changes therefore go through ordinary Gitea pull requests on a repo that
exists anyway (Gitea is the control plane, ADR-0001): review via approvals,
audit via git history, rollback via revert.

## Consequences

- Sub-minute propagation after merge, with no service restarts.
- The git history *is* the audit log of what was blocked, when, and by whom.
- Policy survives cache wipes and restores trivially (it is in the Gitea
  backup).
- Two enforcement dialects must stay semantically aligned by convention; e2e
  scenarios S5 and S10 guard the wiring.
- policy-sync needs a service PAT and webhook plumbing — one more bootstrap
  step (scenario S1).
- New formats add a policy file section plus a policy-sync adapter, keeping
  the same review workflow.
