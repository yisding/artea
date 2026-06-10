# gitea/ — runtime overlay for the stock `gitea/gitea` image

Gitea is Artea's identity provider, PAT issuer, private package store, and UI
(`docs/ARCHITECTURE.md`). It runs the **unmodified upstream Docker image**
(pin: `UPSTREAM`, mirrored as `GITEA_VERSION` in `.env`); everything in this
directory is runtime overlay — no source patches (R7).

```
gitea/
├── app.ini                  # full config, mounted read-only into the container
├── custom/templates/        # Gitea custom-dir template overrides (de-git the UI)
├── patches/                 # quilt-style source-patch escape hatch (EMPTY in v1)
├── scripts/gen-secrets.sh   # generates secrets/ before first start
├── secrets/                 # generated, gitignored: secret_key, internal_token
└── UPSTREAM                 # image pin + bump procedure
```

## How the compose service must consume this overlay

The stock root image has `GITEA_CUSTOM=/data/gitea`, reads its config from
`/data/gitea/conf/app.ini`, and stores all state under the `/data` volume.

| Host path | Container path | Mode |
|---|---|---|
| `./gitea/app.ini` | `/data/gitea/conf/app.ini` | `ro` |
| `./gitea/custom/templates` | `/data/gitea/templates` | `ro` |
| `./gitea/secrets/secret_key` | `/data/gitea/secret_key` | `ro` |
| `./gitea/secrets/internal_token` | `/data/gitea/internal_token` | `ro` |
| `./gitea/secrets/jwt_secret` | `/data/gitea/jwt_secret` | `ro` |
| named volume `gitea-data` | `/data` | rw |

- Image: `gitea/gitea:${GITEA_VERSION}` (exact pin; see `UPSTREAM`). Container
  name `gitea`, internal port 3000, **never published** — only the gateway is.
- Port 22/SSH: do not publish (SSH is disabled in `app.ini`; the image's internal
  sshd is irrelevant without a published port).
- Healthcheck: `GET http://localhost:3000/api/healthz` (registered before the
  auth middleware, so it works despite `REQUIRE_SIGNIN_VIEW = true`).
- **Before first start**: run `gitea/scripts/gen-secrets.sh` (wire into
  `make bootstrap`). Gitea fails fast if the two secret files are missing — that
  is intentional (see "Secrets" below).

### Config injection: mounted app.ini, NOT `GITEA__*` env vars

The image's entrypoint supports `GITEA__section__key=value` env vars, but it
implements them by **rewriting the config file in place**
(`environment-to-ini` → `gitea config edit-ini --in-place --apply-env`). With our
app.ini bind-mounted from the repo that would either fail (read-only mount) or
write values — including secrets — back into the committed file. So the contract
is:

- mount `gitea/app.ini` read-only at `/data/gitea/conf/app.ini`;
- set **no** `GITEA__*` environment variables on the `gitea` service (with none
  set, the entrypoint's rewrite is a no-op and the `ro` mount is safe);
- the full effective config stays reviewable in version control.

### Secrets

`[security]` uses `SECRET_KEY_URI` / `INTERNAL_TOKEN_URI` and `[oauth2]` uses
`JWT_SECRET_URI` (`file:` scheme) instead of inline values. Why: with
`INSTALL_LOCK = true` and any of these unset, Gitea generates the value **and
writes it into app.ini**, which conflicts with the read-only mount — and inline
secrets in a committed file are wrong anyway. (`JWT_SECRET` is generated even
with oauth2 disabled: it doubles as Gitea's general token signing secret, and
its file must hold RawURL-base64 of exactly 32 bytes.)
`scripts/gen-secrets.sh` creates `secrets/secret_key`, `secrets/internal_token`
and `secrets/jwt_secret` (gitignored, idempotent — kept once generated, since
rotating `SECRET_KEY` invalidates sessions and stored 2FA secrets). Note: this is the one secret pair
not living in `.env`; compose cannot mount an env var as a file, and these are
machine-generated rather than operator-chosen, so files are the honest shape.

## What is hidden / disabled, and how

### Via config (`app.ini`) — preferred, zero upgrade drift

- **Web installer** — `INSTALL_LOCK = true`.
- **Self-registration** — `DISABLE_REGISTRATION = true`, registration button off.
- **Anonymous access** — `REQUIRE_SIGNIN_VIEW = true` (R1: none, anywhere).
- **Repo units globally**: issues, ext. issues, wiki, ext. wiki, projects,
  releases, actions — `DISABLED_REPO_UNITS`. This also removes their navbar
  entries and repo tabs (upstream templates check `UnitGlobalDisabled`).
  **`repo.pulls` is intentionally NOT disabled**: policy changes to
  `artea/registry-policy` flow through pull requests (architecture policy model).
  New repos default to code+pulls only (`DEFAULT_REPO_UNITS`).
- **Milestones dashboard** — `SHOW_MILESTONES_DASHBOARD_PAGE = false`.
- **Migrations/imports** — `DISABLE_MIGRATIONS = true`; **mirrors** —
  `[mirror] ENABLED = false`; **Actions** — `[actions] ENABLED = false`.
- **Stars** — `DISABLE_STARS = true`.
- **Org creation by regular users** — `DEFAULT_ALLOW_CREATE_ORGANIZATION = false`
  + `[admin] DISABLE_REGULAR_ORG_CREATION = true` (admins bypass both).
- **SSH / LFS server** — `DISABLE_SSH = true`, `LFS_START_SERVER = false`.
- **RSS/Atom feeds, sitemap, footer version** — `[other]`.
- **Landing page** — `LANDING_PAGE = /artea/-/packages`: anonymous `/` redirects
  there; with sign-in required that becomes the post-login destination.
- **Webhook target allowlist** — `[webhook] ALLOWED_HOST_LIST = policy-sync`.
  Gitea blocks webhooks to private hosts by default, which would silently break
  the policy-sync push webhook; allowlisting *only* `policy-sync` both fixes that
  and prevents webhooks to any other host.
- **Update checker / phone-home** — `[cron.update_checker] ENABLED = false`.

### Via `custom/templates/` — minimal by design, each file is upgrade drift

Every file here shadows its upstream counterpart **wholesale**; upstream changes
to a shadowed file are masked until re-merged. Re-verify on every bump
(procedure in `UPSTREAM`). Current overrides, both copied from upstream
`v1.26.2` with edits marked `ARTEA:`:

| File | Why |
|---|---|
| `base/head_navbar.tmpl` | Replace "Explore" (`/explore/repos`) with "Packages" (`/artea/-/packages`); remove the "+" create dropdown (new repo / migration / new org). Neither is hideable via config. Issues/PRs/milestones links are left to upstream logic since config already controls them. |
| `home.tmpl` | Upstream renders a Gitea marketing page. Replaced with a minimal packages-centric hero. Normally unreachable with our `LANDING_PAGE`; kept as a safety net. Note: its tagline is English-only (upstream localizes via locale keys we cannot extend cleanly). |

### Not hidden — impossible or not worth it without source patches

- **Signed-in dashboard** (`/` for logged-in users): repo feed/heatmap page.
  Hiding it means overriding the large `user/dashboard/*` template tree — too
  much drift for v1. Mitigation: navbar leads with Packages; the dashboard of a
  package-only org is mostly empty.
- **Explore pages** (`/explore/*`): the routes exist even though no navbar item
  points at them. Sign-in is still required (`REQUIRE_SIGNIN_VIEW`).
- **Direct creation URLs** (`/repo/create`, `/org/create`, user settings repo
  tab): hidden from the UI, still routable for users allowed by config. Repo
  creation by regular users is not config-blockable in 1.26 without side effects
  (`MAX_CREATION_LIMIT = 0` would also constrain admin flows); acceptable
  residual surface — a stray repo is harmless and auditable.
- **Git-specific user settings** (SSH/GPG keys page remnants, repo language
  stats, etc.): cosmetic; deferred.
- **PAT expiry dates**: not available upstream; the first planned source patch
  (`patches/README.md`).
- Repos stay functional on purpose — `artea/registry-policy` needs code + PRs.

## SSO (Okta/OIDC) caveat — read before wiring auth

Per the architecture, accounts are "SSO/admin-managed only" and `app.ini` ships
with `DISABLE_REGISTRATION = true`. Gitea counts *first login via OIDC* as a
registration, so with this setting Okta users must be pre-created by an admin.
If first-login auto-provisioning is wanted, change `[service]` to:

```ini
DISABLE_REGISTRATION = false
ALLOW_ONLY_EXTERNAL_REGISTRATION = true
```

which still forbids self-service password signup. Decision belongs to
`docs/guides/okta.md`; the shipped default is the stricter one.

The OIDC auth source itself is data, not config — bootstrap adds it via
`gitea admin auth add-oauth` or the admin UI.

## Upgrades

See `UPSTREAM` for the pinned tag and the full bump procedure (bump pin →
re-verify template overlay → re-verify config keys → `make up` → `make e2e`).
`patches/` stays empty in v1; its README defines the rules for the day it is not.
