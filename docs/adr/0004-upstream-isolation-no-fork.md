# ADR-0004: Upstream isolation — the no-fork rule

Status: accepted (v1)

Amended by the Docker Compose removal: compose is gone; Kubernetes/Helm is the
only runtime (local dev on Colima's built-in k3s). The Gitea config no longer
lives in `gitea/app.ini.template` (deleted) — it is single-sourced in the Helm
chart values (`deploy/helm/artea/values.yaml` `gitea.gitea.config`), still a
config overlay on the stock image. The no-fork principle below is unchanged.

Amended by ADR-0009: the `gitea/patches/` queue is no longer empty — it carries
one source patch (PKCE on OIDC login sources), and Artea now **provides a build
of a patched Gitea image** (`gitea/build-image.sh`, published as
`ghcr.io/yisding/artea-gitea`). It stays opt-in: the stock image remains the
chart default, the patched one is selected via `gitea.image` where a
PKCE-requiring IdP needs it. The no-fork principle holds — one audited,
reversible patch onto a stock upstream tag, with a documented deletion path
(drop it once upstream ships client-side PKCE), not a divergent fork.

## Context

Artea is built on three actively developed upstreams (Gitea, Verdaccio,
devpi). R7 requires that we can pull their improvements indefinitely. Forks
rot: every vendored patch is a permanent rebase tax, and registry software has
a high security-fix cadence we cannot afford to lag behind.

## Decision

No forking, no vendoring, no source patches in v1. Customization happens only
through supported extension surfaces:

1. **Gitea**: stock upstream Docker image by default; behavior via the
   chart-managed config (`deploy/helm/artea/values.yaml` `gitea.gitea.config`),
   appearance via Gitea's supported `custom/` overlay directory (`gitea/custom/`
   templates delivered through the `artea-gitea-custom-templates` ConfigMap).
   (Amended by ADR-0009: an opt-in patched image build covers the one source-level
   gap — see the amendment note above and item 3.)
2. **Verdaccio / devpi**: consumed as released artifacts; our code is plugins
   against their stable plugin APIs (`verdaccio/plugins/*`;
   `devpi/artea_devpi_policy`). The Artea devpi plugin is derived from the
   small `devpi-constrained` plugin but does not vendor or patch devpi itself.
3. **Escape hatch**: `gitea/patches/` — a quilt-style patch queue with an apply
   script and a documented bump procedure. Adding a patch requires an ADR. It now
   carries one patch (PKCE on OIDC login sources, ADR-0009); the deployed Gitea
   image is built from the patched tree (`gitea/build-image.sh`), opt-in over the
   stock default. PAT expiry dates remain a deferred candidate.
4. All version pins live in `.env` / `gitea/UPSTREAM`; floating `latest` is
   forbidden in committed files. The Dockerfiles we own (`devpi/`,
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
- With the patch queue now populated (one PKCE patch, ADR-0009) we knowingly buy
  the rebase tax — but through one audited, reversible mechanism instead of a
  divergent fork, and only for changes overlays cannot reach.
