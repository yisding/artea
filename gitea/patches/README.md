# gitea/patches — source patch queue (escape hatch)

Most of Artea's Gitea customization is runtime overlay: config from the Helm
chart values (`deploy/helm/artea/values.yaml` `gitea.gitea.config`) and templates
from `gitea/custom/`. This directory is the escape hatch (hard rule R7 in
`docs/ARCHITECTURE.md`) for the rare change that overlays cannot reach — so that
the day a source patch becomes unavoidable there is already an agreed mechanism,
and so that "just fork it" never looks like the easy path.

The queue carries source patches for Gitea gaps that runtime overlays cannot
reach: PKCE for OIDC login sources
(`0001-oauth2-send-PKCE-code_challenge-for-OIDC-login-sourc.patch`, ADR-0009) and
server-side OAuth link-account claim binding
(`0002-bind-oauth-link-account-signup-fields-to-claims.patch`, ADR-0010). The
deployed Gitea image is therefore built from the patched tree by
`gitea/build-image.sh` (opt-in; the stock image stays the chart default).

## Policy

- A patch may be added only when the change is impossible via config, the `custom/`
  overlay, plugins, or the gateway — and only with an ADR in `docs/adr/` explaining
  why, plus an upstream issue/PR link (every patch must be on a path to deletion,
  either by upstreaming or by an upstream alternative).
- **PKCE for OIDC login sources** (ADR-0009; upstream go-gitea/gitea#34747,
  #21376) — added because some OIDC providers require PKCE and stock Gitea never
  sends a `code_challenge`.
- **OAuth link-account claim binding** (ADR-0010) — added because the supported
  template overlay can make claim-derived signup fields readonly in the browser,
  but only the Gitea handler can ignore forged `user_name`/`email` fields.
- **Still deferred: PAT expiry dates.** Gitea PATs are currently non-expiring;
  R5 only needs "up to ~1 year", so v1 ships without expiry and this remains a
  candidate the moment policy requires enforced expiry.

## Format (quilt-style)

- One `*.patch` file per logical change, unified diff, applying cleanly against
  the tag named in `gitea/UPSTREAM`.
- `series` lists patch filenames, one per line, in apply order. Blank lines and
  `#` comments are ignored.
- Name patches `NNNN-short-description.patch` (e.g.
  `0001-oauth2-send-PKCE-...patch`) and start each file with a header comment:
  what, why, ADR id, upstream link.

## Applying (the patched image build)

The queue is applied with **`git apply`** (via `apply-patches.sh`), not
`patch -p1`: the patches are git-format and may add new files, which BSD `patch`
(macOS) cannot create from a `/dev/null` diff. `git apply` handles new
files/renames identically on macOS and Linux, so the target must be a git
checkout (it always is — see the bump procedure in `gitea/UPSTREAM`).

```sh
# verify the queue applies cleanly (no files modified):
gitea/patches/apply-patches.sh --check /path/to/gitea-checkout
# apply for a source build:
gitea/patches/apply-patches.sh /path/to/gitea-checkout
```

The checkout must be at exactly the `SOURCE_TAG` from `gitea/UPSTREAM`. The
deployed Gitea image is built from the patched tree by **`gitea/build-image.sh`**
(clone upstream at `SOURCE_TAG` → apply this queue → build Gitea's
`Dockerfile.rootless`), published to `ghcr.io/yisding/artea-gitea` by the `images`
CI workflow. `apply-patches.sh --check` + a recompile is the regression gate for
every rebase.

## Rebasing on upstream bumps

For each upstream bump (see `gitea/UPSTREAM`): run `apply-patches.sh --check`
against the new tag; if a patch no longer applies, regenerate it against the new
tree, keep the same filename, and note the rebase in the patch header. Patches that
landed upstream are deleted from `series` and from disk.
