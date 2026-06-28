# ADR-0010: Patch Gitea to bind OAuth signup identity fields to claims

Status: accepted (v1)
Amends: ADR-0004 (second entry in the `gitea/patches/` queue)

## Context

When `oauth2_client.ENABLE_AUTO_REGISTRATION` is disabled, a first-time SSO user
lands on Gitea's link-account/signup form. Artea's template overlay renders the
username and email from the verified OAuth/OIDC identity as readonly fields when
those values are present, but readonly HTML is only a browser hint. A user can
remove it or submit a crafted `POST /user/link_account_signup` with different
`user_name` or `email` values.

The values that must be trusted live in Gitea's server-side link-account session
as the pending Goth user. Helm config, `custom/` templates, gateway routing, and
plugins cannot change Gitea's registration handler to ignore attacker-controlled
form values.

## Decision

Carry a small Gitea source patch —
`gitea/patches/0002-bind-oauth-link-account-signup-fields-to-claims.patch` — that
re-reads the pending Goth user in `LinkAccountPostRegister` and overwrites the
submitted registration form with claim-derived username and email values when the
provider supplied them.

If a provider did not supply a username or email, Gitea keeps its existing
fallback behavior and accepts the submitted value for that missing field. This
matches the template overlay's readonly gating and avoids making an empty,
required field impossible to complete.

## Consequences

- Crafted signup POST bodies cannot replace an OAuth/OIDC-supplied username or
  email before the local Gitea account is created.
- The template overlay remains useful as a user-interface confirmation, but the
  binding is enforced in Gitea's server-side handler.
- The patch is version-coupled to Gitea's link-account handler and must be
  re-verified with the rest of the patch queue on every `gitea/UPSTREAM` bump.
- Deletion path: drop the patch when upstream Gitea enforces claim-derived fields
  server-side or offers an equivalent supported configuration/API.
