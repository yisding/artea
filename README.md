# Artea

An open-source private package registry with pull-through caching and policy controls —
an open-source alternative to Artifactory, built on [Gitea](https://about.gitea.com),
[Verdaccio](https://verdaccio.org), and [devpi](https://devpi.net).

**v1 formats: npm (JS/TS) and PyPI (Python).**

- One Gitea identity, one long-lived token per user for publish *and* install.
  Okta/OIDC SSO is supported for humans; manual Gitea accounts also work.
- Private packages live in Gitea; public packages are pulled through caching proxies.
- Private names always shadow public ones (dependency-confusion safe by construction).
- Policy-as-code: block public packages/versions by editing one unified
  `policy.toml` in a reviewed Gitea repo (compiled per-ecosystem by policy-sync;
  legacy three-file format still works as a fallback).
- No forks: stock upstream images + config overlays + plugins, so upstream
  improvements keep flowing.

## Quick start

For a guided first deployment and first package publish, start with
[Getting started](docs/guides/getting-started.md).

```sh
cp .env.example .env   # change every placeholder secret for non-throwaway use
make plugins           # build the Verdaccio plugins (mounted, gitignored dist/)
make up                # docker compose up (also generates gitea/secrets/)
make bootstrap         # create admin, org, tokens, policy repo (idempotent)
make smoke             # fast gateway/client sanity checks
make e2e               # run the end-to-end scenario suite
```

Local credentials land in `e2e/tmp/credentials.env` (gitignored). The gateway
is the only exposed service at `http://localhost:8080`; Gitea, Verdaccio,
devpi and policy-sync stay internal.

For production-style Kubernetes installs, use the Helm guide:
[Kubernetes](docs/guides/kubernetes.md), or the turnkey
[AWS EKS](docs/guides/aws-eks.md) walkthrough (cluster + ALB + TLS in one pass).
Expose only the gateway and keep Gitea, Verdaccio, devpi and policy-sync
internal. Replace all `.env` / Helm placeholder secrets before first use; the
dev defaults are for local smoke tests only.

Authoring policy: edit `policy.toml` in the `${ARTEA_NAMESPACE}/registry-policy`
repo via PR (schema: [docs/policy-schema.md](docs/policy-schema.md); operating
and migrating from the legacy format:
[Operations → Policy authoring](docs/guides/operations.md#policy-authoring)).

Client setup:
[npm/pnpm/yarn](docs/guides/clients-npm.md),
[pip/uv/poetry/twine](docs/guides/clients-python.md),
[publishing and tokens](docs/guides/publishing.md).
Architecture: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
