# ADR-0002: Sidecar pull-through proxies with gateway-enforced precedence

Status: accepted (v1)

## Context

Gitea stores private packages but has no pull-through caching of public
registries, and no mechanism to block public packages/versions (R2, R3). We
need both, behind one URL with one credential, for npm and PyPI in v1.

Options considered:

1. **Pulp** as the caching/policy layer. Mature mirroring, but it is a large
   system (Postgres, Redis, workers, own RBAC and storage) that duplicates
   Gitea's auth and artifact storage, has a comparatively weak npm story, and
   would dominate operations for a two-format v1. Rejected for v1.
2. **Native pull-through inside Gitea.** The cleanest end state, but it
   requires source changes to Gitea's package routers, which violates the
   no-fork rule (R7, ADR-0004) on v1's timeline. Deferred to v2.
3. **Per-format sidecar caches** behind a gateway: Verdaccio (npm) and devpi
   (PyPI) are mature, purpose-built pull-through caches with plugin/constraint
   hooks for policy.

## Decision

Option 3. One nginx gateway is the single public entrypoint (`:8080`).
Precedence — private always beats public (R2) — is enforced by mechanism, not
convention:

- **npm**: the client's scope routing sends `@artea/*` to Gitea and everything
  else to Verdaccio; Verdaccio additionally denies access/proxy for `@artea/*`
  (defense in depth) and is read-only.
- **PyPI** (no scopes in PEP 503): the gateway proxies `/pypi/simple/{name}/`
  to Gitea first and only falls back to devpi's `root/constrained` index on a
  Gitea 404. A 200 from Gitea means the public index is never consulted for
  that name.
- Auth is uniform (R1): Verdaccio validates Basic credentials against Gitea
  via a plugin; devpi-bound paths are guarded by nginx `auth_request`
  subrequests to Gitea `/api/v1/user`.

## Consequences

- Standard tooling works unmodified (R4); clients see one URL and one token.
- Two extra services to run, but both hold only disposable caches (ADR-0003).
- Policy lives in two dialects (Verdaccio filter rules, devpi constraints),
  reconciled by the policy repo + policy-sync (ADR-0006).
- The gateway contract is permanent: v2 can replace the sidecars with native
  Gitea pull-through without any client-visible change.
