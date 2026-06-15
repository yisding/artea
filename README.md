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
cp .env.example .env   # change every placeholder secret for non-throwaway use
make plugins           # build the Verdaccio plugins (mounted, gitignored dist/)
make up                # docker compose up (also generates gitea/secrets/)
make bootstrap         # create admin, org, tokens, policy repo (idempotent)
make e2e               # run the end-to-end scenario suite
```

Credentials for local testing land in `e2e/tmp/credentials.env` (gitignored).

For production, expose only the gateway and keep Gitea, Verdaccio, devpi and
policy-sync internal. Replace all `.env` / Helm placeholder secrets before
first use; the dev defaults are for local smoke tests only.

Client setup: see `docs/guides/`. Architecture: see `docs/ARCHITECTURE.md`.
