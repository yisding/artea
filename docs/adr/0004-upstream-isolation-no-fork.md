# ADR-0004: Upstream isolation — the no-fork rule

Status: accepted (v1)

Amended by the Docker Compose removal: compose is gone; Kubernetes/Helm is the
only runtime (local dev on Colima's built-in k3s). The Gitea config no longer
lives in `gitea/app.ini.template` (deleted) — it is single-sourced in the Helm
chart values (`deploy/helm/artea/values.yaml` `gitea.gitea.config`), still a
config overlay on the stock image. The no-fork principle below is unchanged.

## Context

Artea is built on three actively developed upstreams (Gitea, Verdaccio,
devpi). R7 requires that we can pull their improvements indefinitely. Forks
rot: every vendored patch is a permanent rebase tax, and registry software has
a high security-fix cadence we cannot afford to lag behind.

## Decision

No forking, no vendoring, no source patches in v1. Customization happens only
through supported extension surfaces:

1. **Gitea**: stock upstream Docker image; behavior via the chart-managed
   config (`deploy/helm/artea/values.yaml` `gitea.gitea.config`), appearance via
   Gitea's supported `custom/` overlay directory (`gitea/custom/` templates
   delivered through the `artea-gitea-custom-templates` ConfigMap).
2. **Verdaccio / devpi**: consumed as released artifacts; our code is plugins
   against their stable plugin APIs (`verdaccio/plugins/*`;
   `devpi/artea_devpi_policy`). The Artea devpi plugin is derived from the
   small `devpi-constrained` plugin but does not vendor or patch devpi itself.
3. **Escape hatch**: `gitea/patches/` — a quilt-style patch queue, empty in
   v1, with an apply script and a documented bump procedure. Adding a patch
   requires an ADR. First expected candidate: PAT expiry dates.
4. All version pins live in `deploy/helm/artea/values.yaml` / `gitea/UPSTREAM`;
   floating `latest` is forbidden in committed files. The Dockerfiles we own (`devpi/`,
   `policy-sync/`, `scripts/Dockerfile.bootstrap`, and
   `deploy/docker/verdaccio-assets/Dockerfile`) digest-pin their base image
   (`FROM name:tag@sha256:...` — the tag alone is floating; the digest is not).
   Upgrades = bump pin (or re-resolve the digest, see the operations guide),
   `make dev`, `make e2e`.

## Consequences

- Upgrades stay cheap and frequent; security fixes land by changing one pin.
- Some desires are simply out of reach in v1 (e.g. fully removing every git
  UI surface, PAT expiry) — they get documented and deferred rather than
  hacked in.
- Template overlays are version-coupled to Gitea releases and must be
  re-checked on bumps (see operations guide).
- If the patch queue is ever populated, we knowingly buy the rebase tax — but
  through one audited, reversible mechanism instead of a divergent fork.
