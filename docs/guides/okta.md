# Okta SSO (OIDC) for Artea

Humans sign in to Artea (the Gitea UI) through Okta via a generic OpenID
Connect authentication source. Tools never use SSO — they use personal access
tokens (PATs) created after first sign-in (see the end of this guide).

> **PKCE-requiring providers.** Stock Gitea's OIDC login source does not send a
> PKCE `code_challenge` on the authorization request (go-gitea/gitea#34747). An
> OIDC provider that **requires PKCE** for every authorization-code client will
> reject sign-in against the stock image. For those providers, run the patched
> Gitea image built by `gitea/build-image.sh` (ADR-0009), which sends an S256
> `code_challenge`. Okta does not require this; providers with a process-wide
> "PKCE required" setting (e.g. django-oauth-toolkit `PKCE_REQUIRED`) do.

## 1. Create the app in Okta

1. Okta Admin Console → **Applications → Create App Integration**.
2. Sign-in method: **OIDC — OpenID Connect**; application type: **Web Application**.
3. Grant type: **Authorization Code**.
4. Sign-in redirect URI:

   ```
   http://localhost:8080/user/oauth2/okta/callback
   ```

   The path segment `okta` must equal the **Authentication Name** you give the
   source in Gitea (step 2). Use your real public base URL in production —
   it must match Gitea's `ROOT_URL` exactly.
5. Assign the users/groups that should have access.
6. Note the **Client ID** and **Client Secret**.
7. Optional (for team mapping): in your authorization server's claims, add a
   `groups` claim to the ID token (e.g. filter `Matches regex .*`).

The OIDC discovery URL is either the org authorization server:

```
https://<your-okta-domain>/.well-known/openid-configuration
```

or, if you use a custom authorization server (required for custom claims on
some Okta plans):

```
https://<your-okta-domain>/oauth2/default/.well-known/openid-configuration
```

## 2. Add the authentication source in Gitea

### Admin UI

1. Sign in as the bootstrap admin (`ARTEA_ADMIN_USER`; default
   `${ARTEA_NAMESPACE}-admin` when unset).
2. Avatar → **Site Administration** → **Identity & Access →
   Authentication Sources** (`http://localhost:8080/-/admin/auths`) → **Add
   Authentication Source**.
3. Fill in:
   - Authentication type: **OAuth2**
   - OAuth2 provider: **OpenID Connect**
   - Authentication name: `okta` (becomes part of the callback URL)
   - Client ID / Client Secret: from Okta
   - OpenID Connect Auto Discovery URL: the discovery URL above
   - Additional scopes: `profile email` (add `groups` if you mapped the claim)
   - Optional group mapping: *Claim name providing group names* = `groups`,
     and *Map claimed groups to Organization teams*, e.g.:

     ```json
     {"engineering": {"<namespace>": ["developers"]}}
     ```

     Replace `<namespace>` with `ARTEA_NAMESPACE`. (format:
     `{"<okta-group>": {"<org>": ["<team>", ...]}}`; enable
     *Remove users from synchronized teams...* if Okta should also revoke).
4. Save. The login page now shows a "Sign in with okta" button.

### CLI alternative

The same source can be created non-interactively (handy for scripted setups):

```sh
kubectl exec -n artea deploy/artea-gitea -c gitea -- \
  gitea admin auth add-oauth \
    --name okta \
    --provider openidConnect \
    --key '<client-id>' \
    --secret '<client-secret>' \
    --auto-discover-url 'https://<your-okta-domain>/.well-known/openid-configuration' \
    --scopes openid --scopes profile --scopes email --scopes groups \
    --group-claim-name groups \
    --group-team-map '{"engineering": {"<namespace>": ["developers"]}}'
```

`gitea admin auth list` / `update-oauth --id <n> ...` manage it afterwards.

## 3. Auto-registration of SSO users

With the hardening below, the first Okta sign-in creates the Gitea account
automatically (no manual registration step, no self-registration form). The
relevant `app.ini` keys:

```ini
[service]
; no self-service registration form...
DISABLE_REGISTRATION = false
; ...but accounts may be created through an external auth source (Okta)
ALLOW_ONLY_EXTERNAL_REGISTRATION = true

[oauth2_client]
; create the account on first OIDC login without a confirmation form
ENABLE_AUTO_REGISTRATION = true
; derive the Gitea username from the OIDC preferred_username claim
; (other values: nickname [default], email, userid)
USERNAME = preferred_username
; auto-link by email if an account with the same address already exists
; (values: disabled, login, auto)
ACCOUNT_LINKING = auto
```

Note `DISABLE_REGISTRATION` must stay `false` — setting it `true` disables
*all* registration including SSO auto-registration.

## 4. Recommended hardening: disable password login

Once SSO works (and after the bootstrap hook Job has run — it runs automatically
on `make dev` / `helm upgrade --install`, and uses the admin password over Basic
auth), turn off password-based entry points:

```ini
[service]
; hide the username/password form on the login page; SSO buttons remain
ENABLE_PASSWORD_SIGNIN_FORM = false
; reject HTTP Basic with account *passwords*. PATs are unaffected:
; Basic with user:PAT is verified as a token before this setting applies,
; so npm/pip/twine keep working.
ENABLE_BASIC_AUTHENTICATION = false
```

Keep one local admin (`ARTEA_ADMIN_USER`) reachable for break-glass via
`gitea admin` CLI inside the container; with the form disabled, day-to-day
admin actions should also go through an SSO account that has been granted
admin (or the Okta `admin-group` claim mapping).

## 5. The PAT-after-SSO flow

SSO users have no Gitea password, so package tooling cannot authenticate as
them with username/password — and that is by design. The flow is:

1. Sign in to `http://localhost:8080` with Okta.
2. Avatar → **Settings** → **Applications** → generate a token with the
   **user** and **organization** permissions (`read:user`, `read:organization`)
   plus the **package** permission (`read:package` or `write:package`).
3. Put the token in `.npmrc` / `~/.netrc` / `.pypirc` as described in
   [clients-npm.md](clients-npm.md) and [clients-python.md](clients-python.md).

The web UI is the only token-creation path for SSO users: Gitea's token REST
API (`POST /api/v1/users/{username}/tokens`) requires Basic auth, which SSO
accounts cannot use. Tokens keep working independently of the Okta session;
deactivating a user in Okta does **not** revoke their PATs — offboarding must
also delete (or have an admin delete) the user's tokens in Gitea.
