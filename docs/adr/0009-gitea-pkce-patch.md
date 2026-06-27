# ADR-0009: Patch Gitea to send PKCE on OIDC login sources

Status: accepted (v1). First entry in the `gitea/patches/` queue; amends ADR-0004.

## Context

R1 SSO uses Gitea's OIDC login source (Gitea acting as the OAuth2 *client*). Some
OIDC identity providers **require PKCE for every authorization-code client**,
including confidential ones — for example a django-oauth-toolkit provider with
`PKCE_REQUIRED` (a process-wide setting with no per-client opt-out). Against such
an IdP a stock Gitea cannot complete SSO: it never sends a `code_challenge` on
the authorization request. This is an open upstream limitation, not a
misconfiguration — go-gitea/gitea#34747, and feature request #21376 (open since
2022, no merged fix).

It cannot be fixed through config, the `custom/` overlay, a plugin, or the
gateway — the gap is in Gitea's OAuth2 client code. That is exactly the condition
ADR-0004 reserves the `gitea/patches/` escape hatch for.

## Decision

Carry a single source patch — `gitea/patches/0001-oauth2-send-PKCE-code_challenge-for-OIDC-login-sourc.patch` —
that makes Gitea's `openidConnect` login source generate a per-request S256
verifier, append `code_challenge` to the authorization redirect, and inject the
stored verifier on the callback so goth forwards it to the token exchange. It is
gated to the `openidConnect` provider via a provider `SupportsPKCE()` capability;
no settings, schema, or dependency changes (`golang.org/x/oauth2` already ships
the helpers).

Artea therefore **provides a reproducible build of a patched Gitea image**
(`gitea/build-image.sh`): clone upstream Gitea at the `SOURCE_TAG`/`SOURCE_COMMIT` pins in
`gitea/UPSTREAM`, verify the tag resolves to that commit, apply the patch queue (`apply-patches.sh`, via `git apply`),
and build Gitea's own `Dockerfile.rootless`. CI publishes it as
`ghcr.io/yisding/artea-gitea`.

It is **opt-in**, not the chart default: the stock Gitea image stays the default
(so installs that don't face a PKCE-mandating IdP, and the e2e/CI path, are
unchanged), and deployments that need PKCE select the patched image via
`gitea.image` — `make dev` builds and uses it locally, and the production deploy
points at the published image. The patch's deletion path means this opt-in is
temporary: once Gitea ships client-side PKCE, drop the patch and the build.

## Consequences

- **Amends ADR-0004:** the patch queue is no longer empty, and Artea now builds
  the Gitea image rather than running the stock one. The no-fork *principle*
  holds — this is one audited, reversible patch applied onto a stock upstream
  tag (not a divergent fork), with a documented deletion path.
- **Deletion path:** the patch is on the way upstream (#34747 / #21376). The
  moment a Gitea release ships client-side PKCE, drop the patch and return to the
  stock image — a one-line `gitea/UPSTREAM` change.
- **Rebase tax:** each Gitea version bump must re-verify the patch applies
  (`apply-patches.sh --check`) and the package compiles. The patch is small (one
  self-contained `oauth2/pkce.go` plus a few call-site lines) and its integration
  point has been stable across the 1.26 line, so the tax is low. Porting onto
  v1.26.2 needed only a module-path rename (`gitea.dev` → `code.gitea.io/gitea`).
- **Build cost:** a Gitea image build (Go + frontend) joins the image pipeline,
  cached in CI.
- **Verified:** the patch's unit tests pass and the package compiles on stable
  v1.26.2; end-to-end, the patched Gitea sends `code_challenge` with
  `code_challenge_method=S256` on the authorize request (confirmed against a live
  PKCE-requiring IdP).
