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
  `policy.toml` in a reviewed Gitea repo (compiled per-ecosystem by policy-sync).
- No forks: stock upstream images + config overlays + plugins, so upstream
  improvements keep flowing.

## Quick start

Artea runs on Kubernetes only; for local dev that means
[Colima](https://github.com/abiosoft/colima)'s built-in k3s. For a guided first
deployment and first package publish, start with
[Getting started](docs/guides/getting-started.md); for the Colima specifics see
[Local development](docs/guides/local-dev.md).

```sh
colima start --kubernetes   # k3s sharing the docker image store (--cpu 4 --memory 8 for headroom)
make dev                    # build the four images, helm-install the chart, port-forward the gateway
make e2e                    # run the end-to-end scenario suite (smoke + S1-S20)
```

`make dev` builds the Artea images, `helm upgrade --install`s the chart (the
bootstrap hook Job creates the admin, org, tokens and policy repo), and
port-forwards the gateway to `http://localhost:8080`. Local credentials land in
`e2e/tmp/credentials.env` (gitignored). The gateway is the only exposed service;
Gitea, Verdaccio, devpi and policy-sync stay internal.

For production-style Kubernetes installs, use the Helm guide:
[Kubernetes](docs/guides/kubernetes.md), or the turnkey
[AWS EKS](docs/guides/aws-eks.md) walkthrough (cluster + ALB + TLS in one pass).
Expose only the gateway and keep Gitea, Verdaccio, devpi and policy-sync
internal. Replace all Helm placeholder secrets before first use; the dev
defaults are for local smoke tests only.

Authoring policy: edit `policy.toml` in the `${ARTEA_NAMESPACE}/registry-policy`
repo via PR (schema: [docs/policy-schema.md](docs/policy-schema.md); operating:
[Operations → Policy authoring](docs/guides/operations.md#policy-authoring)).

Client setup:
[npm/pnpm/yarn](docs/guides/clients-npm.md),
[pip/uv/poetry/twine](docs/guides/clients-python.md),
[publishing and tokens](docs/guides/publishing.md).
Architecture: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
