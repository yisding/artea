# gitea/ — runtime overlay for the stock `gitea/gitea` image

Gitea is Artea's identity provider, PAT issuer, private package store, and UI
(`docs/ARCHITECTURE.md`). It runs the **unmodified upstream Docker image**
(pin: `UPSTREAM`, mirrored as `GITEA_VERSION` in `.env`); everything in this
directory is runtime overlay — no source patches (R7).

```
gitea/
├── custom/templates/        # template overrides, delivered via ConfigMap
├── patches/                 # quilt-style source-patch escape hatch (EMPTY in v1)
└── UPSTREAM                 # image pin + bump procedure
```

The effective Gitea config now lives in the Helm chart values
(`deploy/helm/artea/values.yaml`, key `gitea.gitea.config`) — a full semantic
translation of the old `app.ini`. The official Gitea subchart owns secrets and
filesystem paths; this directory only carries the template overlay and the
(empty) patch queue.

## How Kubernetes consumes this overlay

The official Gitea subchart runs the stock rootless image (`gitea/gitea`, exact
pin; see `UPSTREAM`) as the `artea-gitea` Deployment (container `gitea`) in
namespace `artea`. Port 3000 is **never exposed** — only the gateway is, and SSH
is disabled in config. The Artea `custom/` overlay is delivered by the
`artea-gitea-custom-templates` ConfigMap, mounted over `/data/gitea/templates`.
The effective config comes from `gitea.gitea.config` in
`deploy/helm/artea/values.yaml`; secrets are chart-generated (see "Secrets").

### Config injection: chart-managed `gitea.gitea.config`

The chart manages config via `gitea.gitea.config`, from which it generates the
in-image `app.ini`. A few values are layered on top as templated
`additionalConfigFromEnvs` overrides — `ROOT_URL` / `DOMAIN` / `LANDING_PAGE`
derived from `global.baseUrl`. See `deploy/helm/artea/values.yaml`.

### Secrets

The Gitea subchart generates `SECRET_KEY`, `INTERNAL_TOKEN`, and `JWT_SECRET`;
the bootstrap admin credential comes from the `artea-admin` chart Secret.
Rotating `SECRET_KEY` invalidates existing sessions and stored 2FA secrets, so
the chart keeps it stable once generated.

## What is hidden / disabled, and how

### Via config (`gitea.gitea.config` in values.yaml) — preferred, zero upgrade drift

- **Web installer** — `INSTALL_LOCK = true`.
- **Self-registration** — `DISABLE_REGISTRATION = true`, registration button off.
- **Anonymous access** — `REQUIRE_SIGNIN_VIEW = true` (R1: none, anywhere).
- **Repo units globally**: issues, ext. issues, wiki, ext. wiki, projects,
  releases, actions — `DISABLED_REPO_UNITS`. This also removes their navbar
  entries and repo tabs (upstream templates check `UnitGlobalDisabled`).
  **`repo.pulls` is intentionally NOT disabled**: policy changes to
  `${ARTEA_NAMESPACE}/registry-policy` flow through pull requests (architecture policy model).
  New repos default to code+pulls only (`DEFAULT_REPO_UNITS`).
- **Milestones dashboard** — `SHOW_MILESTONES_DASHBOARD_PAGE = false`.
- **Migrations/imports** — `DISABLE_MIGRATIONS = true`; **mirrors** —
  `[mirror] ENABLED = false`; **Actions** — `[actions] ENABLED = false`.
- **Stars** — `DISABLE_STARS = true`.
- **Org creation by regular users** — `DEFAULT_ALLOW_CREATE_ORGANIZATION = false`
  + `[admin] DISABLE_REGULAR_ORG_CREATION = true` (admins bypass both).
- **SSH / LFS server** — `DISABLE_SSH = true`, `LFS_START_SERVER = false`.
- **RSS/Atom feeds, sitemap, footer version** — `[other]`.
- **Landing page** — `LANDING_PAGE = /${ARTEA_NAMESPACE}/-/packages`: anonymous `/` redirects
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
| `base/head_navbar.tmpl.template` | Replace "Explore" (`/explore/repos`) with "Packages" (`/${ARTEA_NAMESPACE}/-/packages`); add a "Client setup" link to the bootstrap-seeded registry guide; remove the "+" create dropdown (new repo / migration / new org). Neither is hideable via config. Issues/PRs/milestones links are left to upstream logic since config already controls them. |
| `home.tmpl.template` | Upstream renders a Gitea marketing page. Replaced with a minimal packages-centric hero with package and client-setup links. Normally unreachable with our `LANDING_PAGE`; kept as a safety net. Note: its tagline is English-only (upstream localizes via locale keys we cannot extend cleanly). |

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
- Repos stay functional on purpose — `${ARTEA_NAMESPACE}/registry-policy` needs code + PRs.

## SSO (Okta/OIDC) caveat — read before wiring auth

Per the architecture, accounts are "SSO/admin-managed only" and the chart config
(`gitea.gitea.config`) ships with `DISABLE_REGISTRATION = true`. Gitea counts
*first login via OIDC* as a
registration, so with this setting Okta users must be pre-created by an admin.
If first-login auto-provisioning is wanted, change `[service]` to:

```ini
DISABLE_REGISTRATION = false
ALLOW_ONLY_EXTERNAL_REGISTRATION = true
```

which still forbids self-service password signup. Decision belongs to
`docs/guides/okta.md`; the shipped default is the stricter one.

The OIDC auth source itself is data, not config — bootstrap adds it via
`kubectl exec -n artea deploy/artea-gitea -c gitea -- gitea admin auth add-oauth`
or the admin UI.

## Upgrades

See `UPSTREAM` for the pinned tag and the full bump procedure (bump pin →
re-verify template overlay → re-verify config keys → `make dev` → `make e2e`).
`patches/` stays empty in v1; its README defines the rules for the day it is not.
