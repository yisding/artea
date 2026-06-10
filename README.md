# Artea

An open-source private package registry with pull-through caching and policy controls —
an open-source alternative to Artifactory, built on [Gitea](https://about.gitea.com),
[Verdaccio](https://verdaccio.org), and [devpi](https://devpi.net).

**v1 formats: npm (JS/TS) and PyPI (Python).**

- One login (Okta/OIDC via Gitea), one long-lived token per user for publish *and* install.
- Private packages live in Gitea; public packages are pulled through caching proxies.
- Private names always shadow public ones (dependency-confusion safe by construction).
- Policy-as-code: block public packages/versions via a reviewed Gitea repo.
- No forks: stock upstream images + config overlays + plugins, so upstream
  improvements keep flowing.

## Quick start

```sh
cp .env.example .env   # edit passwords
make up                # docker compose up
make bootstrap         # create admin, org, tokens, policy repo
make e2e               # run the end-to-end scenario suite
```

Client setup: see `docs/guides/`. Architecture: see `docs/ARCHITECTURE.md`.
