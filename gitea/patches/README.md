# gitea/patches — source patch queue (escape hatch, empty by design)

Artea runs the **stock upstream `gitea/gitea` image with zero source patches**
(hard rule R7 in `docs/ARCHITECTURE.md`). All v1 customization is runtime overlay:
config/templates rendered from `gitea/app.ini.template` and `gitea/custom/`.
This directory exists so that the day a source
patch becomes unavoidable, there is already an agreed mechanism — and so that "just
fork it" never looks like the easy path.

## Policy

- **v1 ships with zero patches.** `series` is empty and must stay empty until a
  patch is accepted.
- A patch may be added only when the change is impossible via config, the `custom/`
  overlay, plugins, or the gateway — and only with an ADR in `docs/adr/` explaining
  why, plus an upstream issue/PR link (every patch must be on a path to deletion,
  either by upstreaming or by an upstream alternative).
- **First planned patch: PAT expiry dates.** Gitea PATs are currently non-expiring;
  R5 only needs "up to ~1 year", so v1 ships without expiry and this is the first
  candidate the moment policy requires enforced expiry.

## Format (quilt-style)

- One `*.patch` file per logical change, unified diff, `-p1` strip level, applying
  cleanly against the tag named in `gitea/UPSTREAM`.
- `series` lists patch filenames, one per line, in apply order. Blank lines and
  `#` comments are ignored.
- Name patches `NNNN-short-description.patch` (e.g. `0001-pat-expiry.patch`) and
  start each file with a header comment: what, why, ADR id, upstream link.

## Applying (future source builds)

```sh
# verify the queue applies cleanly (no files modified):
gitea/patches/apply-patches.sh --check /path/to/gitea-checkout
# apply for a source build:
gitea/patches/apply-patches.sh /path/to/gitea-checkout
```

The checkout must be at exactly the `SOURCE_TAG` from `gitea/UPSTREAM`. Once any
patch exists, the deployment switches from the stock image to an image built from
the patched tree (a `gitea/Dockerfile` to be added alongside the first patch), and
`make e2e` becomes the regression gate for every rebase.

## Rebasing on upstream bumps

For each upstream bump (see `gitea/UPSTREAM`): run `apply-patches.sh --check`
against the new tag; if a patch no longer applies, regenerate it against the new
tree, keep the same filename, and note the rebase in the patch header. Patches that
landed upstream are deleted from `series` and from disk.
