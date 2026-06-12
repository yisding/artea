# ADR-0002: Sidecar pull-through proxies with gateway-enforced precedence

Status: accepted (v1); amended 2026-06-10 — npm scope routing moved from the
client to the gateway (see Amendment below; backward compatible)

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

- **npm**: the gateway routes the configured private scope
  (`@${ARTEA_NAMESPACE}`, default `@artea`) server-side: a regex location peels
  `/npm/@${ARTEA_NAMESPACE}/...` and the dist-tag API
  `/npm/-/package/@${ARTEA_NAMESPACE}/...` off the Verdaccio route and proxies
  them to Gitea's `/api/packages/${ARTEA_NAMESPACE}/npm/...`,
  explicitly matching npm's literal and encoded `@` / scope-separator spellings
  and taking the forwarded path from a `map` over the raw `$request_uri` so
  npm's `%2f`/`%40` encodings reach Gitea byte-for-byte (a scoped-location
  match whose raw form matches neither pattern is rejected with 400). A scope
  match, never a 404-fallback: private-scope names are structurally unable to reach
  Verdaccio or npmjs. Everything else under `/npm/` goes to Verdaccio, which
  additionally denies access/proxy for `@${ARTEA_NAMESPACE}/*` (defense in depth) and is
  read-only.
  Client-side scope routing (`@${ARTEA_NAMESPACE}:registry=...`) remains supported as
  optional legacy.
- **PyPI** (no scopes in PEP 503): the gateway proxies `/pypi/simple/{name}/`
  to Gitea first and only falls back on a Gitea 404. The fallback goes through
  policy-sync, which asks devpi's `root/constrained` index and applies any
  PyPI age gate before returning public links. A 200 from Gitea means the
  public index is never consulted for that name.
- Auth is uniform (R1): Verdaccio validates Basic credentials against Gitea
  via a plugin; devpi-bound paths are guarded by nginx `auth_request`
  subrequests to Gitea `/api/v1/user`.

## Consequences

- Standard tooling works unmodified (R4); clients see one URL and one token —
  for npm, since the 2026-06-10 amendment, one registry URL and one credential
  value instead of two registries with two credential lines.
- Two extra services to run, but both hold only disposable caches (ADR-0003).
- Policy lives in two dialects (Verdaccio filter rules, devpi constraints),
  reconciled by the policy repo + policy-sync (ADR-0006).
- The gateway contract is permanent: v2 can replace the sidecars with native
  Gitea pull-through without any client-visible change. Gateway scope routing
  strengthens this: npm clients no longer encode any backend path in their
  config.

## Amendment (2026-06-10): gateway scope routing for npm

As originally accepted, npm precedence relied on the client's scope routing
(`@artea:registry=` in `.npmrc` for the default namespace); the gateway's only
npm role was the auth guard. The gateway now enforces the same precedence
server-side (mechanism in the Decision above), shrinking the client contract to
one registry URL plus one credential value. Backward compatible: the legacy
two-registry `.npmrc` behaves identically (it reaches Gitea directly, bypassing
the scope match), and Verdaccio's configured private-scope deny rule stays as
defense in depth. The Decision and Consequences sections were updated in place
to describe the amended mechanism; ARCHITECTURE.md and
docs/guides/clients-npm.md carry the new client contract (including the
npm-publish credential-preflight caveat).
