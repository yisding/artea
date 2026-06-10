# ADR-0004: Upstream isolation — the no-fork rule

Status: accepted (v1)

## Context

Artea is built on three actively developed upstreams (Gitea, Verdaccio,
devpi). R7 requires that we can pull their improvements indefinitely. Forks
rot: every vendored patch is a permanent rebase tax, and registry software has
a high security-fix cadence we cannot afford to lag behind.

## Decision

No forking, no vendoring, no source patches in v1. Customization happens only
through supported extension surfaces:

1. **Gitea**: stock upstream Docker image; behavior via mounted
   `gitea/app.ini`, appearance via Gitea's supported `custom/` overlay
   directory (`gitea/custom/`).
2. **Verdaccio / devpi**: consumed as released artifacts; our code is plugins
   against their stable plugin APIs (`verdaccio/plugins/*`; devpi needs the
   `devpi-constrained` plugin only, no custom plugin in v1).
3. **Escape hatch**: `gitea/patches/` — a quilt-style patch queue, empty in
   v1, with an apply script and a documented bump procedure. Adding a patch
   requires an ADR. First expected candidate: PAT expiry dates.
4. All version pins live in `.env` / `gitea/UPSTREAM`; floating `latest` is
   forbidden in committed files. Upgrades = bump pin, `make up`, `make e2e`.

## Consequences

- Upgrades stay cheap and frequent; security fixes land by changing one pin.
- Some desires are simply out of reach in v1 (e.g. fully removing every git
  UI surface, PAT expiry) — they get documented and deferred rather than
  hacked in.
- Template overlays are version-coupled to Gitea releases and must be
  re-checked on bumps (see operations guide).
- If the patch queue is ever populated, we knowingly buy the rebase tax — but
  through one audited, reversible mechanism instead of a divergent fork.
