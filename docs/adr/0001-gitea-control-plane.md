# ADR-0001: Gitea as control plane and source of truth

Status: accepted (v1)

## Context

Artea needs, in one system: identity with Okta/OIDC SSO (R1), long-lived
tokens that work for both publish and pull (R5, R6), private npm and PyPI
endpoints that stock clients understand (R4), durable artifact storage, an
admin UI, and an auditable place to keep policy. Building or assembling these
separately (auth service + token store + object store + per-format registry +
UI) multiplies operational surface and credential systems.

Gitea already ships all of it: OIDC authentication sources, scoped personal
access tokens (`read:package`/`write:package`, write implies read),
org/team-based authorization, native npm and PyPI package registries
(`/api/packages/{owner}/npm/`, `/api/packages/{owner}/pypi/`) plus ~20 more
formats for future growth, git hosting for the policy repo, webhooks, and a UI
— in a single small Go binary with a healthy upstream.

## Decision

Gitea is the control plane and the single source of truth. All identity, all
credentials (PATs), all authorization, all private artifacts, and the policy
repo live in Gitea. Every other component (gateway, Verdaccio, devpi,
policy-sync) is stateless or holds only disposable caches, and validates every
credential against Gitea APIs. Git-hosting features are hidden via configuration
and template overlay, not removed.

## Consequences

- One backup target (the Gitea data volume), one account system, one token UX.
- Adding a future format starts from a registry endpoint Gitea already has.
- Gitea becomes the availability bottleneck: if it is down, even cached public
  installs fail (auth is validated there). Accepted for v1.
- We inherit Gitea's package-API semantics and limits; fixes go upstream or
  wait (no fork — see ADR-0004).
- Gitea's PATs are non-expiring; acceptable under R5, revisit in v2
  (candidate for the patch queue).
