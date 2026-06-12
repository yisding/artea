# ADR-0005: Single configured organization as the v1 namespace

Status: accepted (v1)

## Context

Gitea namespaces packages per owner: `/api/packages/{owner}/...`. We must
choose how many owners exist in v1. npm has scopes (`@owner/*` maps cleanly to
an owner), but PEP 503 has no scopes: a Python client gets exactly one index
URL, so multiple Python-package owners would need per-owner index URLs or a
merging gateway — both of which complicate the "single index, 404-fallback,
private-shadows-public" precedence mechanism (ADR-0002).

## Decision

v1 uses a single configured Gitea organization, `ARTEA_NAMESPACE` (default
`artea`), as the only private namespace:

- npm scope = org: private packages are `@${ARTEA_NAMESPACE}/*`, published to
  and installed from `/api/packages/${ARTEA_NAMESPACE}/npm/`.
- Python: the gateway's `/pypi/simple/` checks exactly one Gitea owner
  (`ARTEA_NAMESPACE`); all private Python packages live there, in one flat
  name space.
- Access control within the org uses Gitea teams (e.g. mapped from Okta groups).

Multi-org Python namespacing is explicitly deferred — it is a known-hard
problem (PyPI has no scopes) and nothing in v1 may preclude solving it later
(e.g. per-org index URLs `/pypi/{org}/simple/` in the gateway).

## Consequences

- Trivially predictable URLs and a single dependency-confusion boundary.
- Coarse isolation: anyone with org package write access can publish any
  private name. Acceptable at v1 scale; teams refine this only partially.
- The org name is a deployment-time contract (`ARTEA_NAMESPACE`). Changing it
  later changes client-facing package coordinates and endpoint URLs, so treat
  renames as a migration.
- New formats added later follow the same rule: enable the format under the
  configured org (see the scale-out recipe in ARCHITECTURE.md).
