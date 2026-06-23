# Architecture Decision Records

These ADRs capture the load-bearing design decisions behind Artea. They are the
durable record of *why* the architecture in [`../ARCHITECTURE.md`](../ARCHITECTURE.md)
looks the way it does; when implementation reality forces a deviation, it is
recorded here.

Each ADR carries a `Status` line and, where applicable, structured relationship
header lines (`Amends`, `Amended-by`, `Extends`, `Extended-by`, `Supersedes`,
`Superseded-by`) so relationships between decisions are greppable.

| ADR | Title | Status |
|-----|-------|--------|
| [0001](0001-gitea-control-plane.md) | Gitea as control plane and source of truth | accepted (v1) |
| [0002](0002-sidecar-pull-through-proxies.md) | Sidecar pull-through proxies with gateway-enforced precedence | accepted (v1); amended (gateway scope routing for npm) |
| [0003](0003-artifacts-in-gitea-caches-disposable.md) | All private artifacts stored in Gitea; caches are disposable | accepted (v1) |
| [0004](0004-upstream-isolation-no-fork.md) | Upstream isolation — the no-fork rule | accepted (v1); amended by ADR-0009 |
| [0005](0005-single-org-namespace.md) | Single configured organization as the v1 namespace | accepted (v1) |
| [0006](0006-policy-as-code.md) | Policy as code in a reviewed Gitea repo | accepted (v1); authoring format superseded by ADR-0007 |
| [0007](0007-unified-policy-schema.md) | Unified cross-ecosystem policy schema | accepted (v1); extends ADR-0006 |
| [0009](0009-gitea-pkce-patch.md) | Patch Gitea to send PKCE on OIDC login sources | accepted (v1); amends ADR-0004 |

## The 0008 gap

There is no ADR-0008. The number is intentionally unused — it was a parked draft
that was withdrawn and superseded by [ADR-0009](0009-gitea-pkce-patch.md). The
numbers are not renumbered, so existing references to ADR-0009 stay stable.
