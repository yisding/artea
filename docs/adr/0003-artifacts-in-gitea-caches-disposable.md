# ADR-0003: All private artifacts stored in Gitea; caches are disposable

Status: accepted (v1)

## Context

With three artifact-touching services (Gitea, Verdaccio, devpi) we must decide
where private packages physically live. Splitting storage (e.g. publishing
Python packages into devpi, which supports it) would mean multiple backup
targets, multiple permission models, and private bytes sitting in components
that also talk to public registries.

## Decision

Private artifacts — npm tarballs and Python wheels/sdists — are stored only in
Gitea. Publishes land in Gitea endpoints (`npm publish` to `/npm/`, which the
gateway scope-routes to `/api/packages/${ARTEA_NAMESPACE}/npm/` — see the
ADR-0002 amendment; the legacy direct `@${ARTEA_NAMESPACE}:registry` URL still
works — and `twine upload` to
`/api/packages/${ARTEA_NAMESPACE}/pypi/`). Verdaccio and devpi hold
nothing but re-fetchable copies of public packages; Verdaccio is configured
read-only and devpi is never a publish target.

## Consequences

- Backup = the Gitea data volume, full stop. Cache volumes may be wiped at any
  time (upgrades, corruption, disk pressure) with no data loss — only a warm-up
  cost.
- Dependency confusion resistance: private package content never transits the
  components that talk to npmjs/PyPI, and a private name in Gitea shadows the
  public one by mechanism (ADR-0002).
- Strengthens the v2 path: retiring a sidecar cache cannot orphan artifacts.
- All artifact-storage scaling (disk, quotas, GC) is a Gitea-only concern.
