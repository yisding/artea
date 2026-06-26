# Getting started

This guide gets a local Artea registry running, signs in with the bootstrap
accounts, and publishes one private package. Use it when you want the shortest
path from clone to a working npm/PyPI registry.

Artea runs on Kubernetes only; for local dev that means Colima's built-in k3s.
Read this once for the product flow, then use [local-dev.md](local-dev.md) for
the Colima quickstart and [kubernetes.md](kubernetes.md) for the full
cluster-specific install steps.

## What you get

- Gateway at `http://localhost:8080`, the only public entrypoint.
- Gitea as identity, PAT issuer, policy repo, and private package store.
- Verdaccio as the authenticated npm pull-through cache.
- devpi as the authenticated PyPI pull-through cache.
- `policy-sync`, which copies reviewed registry policy from Gitea into the
  caches.

Okta is not required to try or run Artea. By default, users are created by the
Gitea admin and package tools authenticate as `username:Gitea-PAT`. Okta/OIDC
can be added later for human sign-in.

## Prerequisites

- [Colima](https://github.com/abiosoft/colima) and the Docker CLI.
  `colima start --kubernetes` gives you a local k3s cluster that shares
  Colima's image store, so locally-built images are visible without a registry.
- `helm` ≥ 3.14 and `kubectl`.
- `make`.
- `pnpm`, because the Verdaccio plugins are plain TypeScript packages.
- npm if you want to run the npm example.
- Python packaging tools if you want to run the Python publishing example.

## 1. Start the local stack

```sh
colima start --kubernetes
make dev
```

`make dev` builds the four Artea images (`:local`), `helm upgrade --install`s
the chart with `values-local.yaml`, and port-forwards the gateway to
`http://localhost:8080`. The chart generates the Gitea secrets, renders the
runtime configs into ConfigMaps, and runs the idempotent bootstrap hook Job
that creates the admin, namespace org, policy repo, webhook, teams, demo user
(`dev1`), and PATs.

For local throwaway use, `values-local.yaml` sets
`secrets.allowDevPlaceholders: true`, which accepts the `change-me-*`
placeholder secrets. Any non-throwaway deployment must instead set real
`secrets:` values (see [kubernetes.md](kubernetes.md)).

The bootstrap Job emits a framed credentials block in its logs. `make e2e`
extracts it to `e2e/tmp/credentials.env`; to load credentials without running
the full suite, run `scripts/k8s-e2e.sh` (which performs the same extraction)
or read them from `kubectl -n artea logs job/artea-bootstrap` directly.

Load them in a shell:

```sh
source e2e/tmp/credentials.env
```

Useful values:

| Variable | Meaning |
|----------|---------|
| `GATEWAY_URL` | Local gateway URL, normally `http://localhost:8080` |
| `ARTEA_NAMESPACE` | Gitea org and private npm scope, default `artea` |
| `ARTEA_ADMIN_USER` / `ARTEA_ADMIN_PASSWORD` | Bootstrap admin login |
| `DEV1_USER` / `DEV1_TOKEN` | Demo package publisher account and PAT |

Sign in at `http://localhost:8080` as the admin or as `dev1`.

## 2. Verify public pull-through

Both public package caches require auth. With the demo PAT:

```sh
curl -u "${DEV1_USER}:${DEV1_TOKEN}" "${GATEWAY_URL}/npm/left-pad"
curl -u "${DEV1_USER}:${DEV1_TOKEN}" "${GATEWAY_URL}/pypi/simple/six/"
```

The default seed policy blocks nothing and has no upstream age gate, so public
packages should resolve through the caches once policy-sync has completed.

## 3. Publish and install an npm package

Create a throwaway package under the configured private scope:

```sh
source e2e/tmp/credentials.env

mkdir -p /tmp/artea-npm-demo
cd /tmp/artea-npm-demo

NPM_AUTH="$(printf '%s:%s' "${DEV1_USER}" "${DEV1_TOKEN}" | base64 | tr -d '\n')"
cat > .npmrc <<EOF
registry=http://localhost:8080/npm/
//localhost:8080/:_auth=${NPM_AUTH}
//localhost:8080/npm/:_auth=${NPM_AUTH}
EOF

NPM_PACKAGE="@${ARTEA_NAMESPACE}/hello-$(date +%s)"
printf 'module.exports = "hello from Artea";\n' > index.js
npm init -y >/dev/null
npm pkg set "name=${NPM_PACKAGE}" version=0.1.0 main=index.js

npm publish
npm view "${NPM_PACKAGE}" versions

mkdir -p /tmp/artea-npm-consumer
cd /tmp/artea-npm-consumer
cp /tmp/artea-npm-demo/.npmrc .
npm init -y >/dev/null
npm install "${NPM_PACKAGE}"
```

The private scope routes to Gitea. Unscoped packages route to Verdaccio and are
read-only, so unscoped `npm publish` is rejected by design.

For real projects, copy the `.npmrc` shape from
[clients-npm.md](clients-npm.md). Keep both `_auth` lines when publishing; npm
needs the `/npm/`-scoped line for its local publish preflight.

## 4. Use Python packages

Configure Basic auth once with `~/.netrc`. If you already have a `localhost`
entry, update it instead of appending a second one.

```sh
source e2e/tmp/credentials.env

cat >> ~/.netrc <<EOF
machine localhost
login ${DEV1_USER}
password ${DEV1_TOKEN}
EOF
chmod 600 ~/.netrc
```

Install a public package through the authenticated PyPI pull-through cache:

```sh
python -m pip install --index-url "${GATEWAY_URL}/pypi/simple/" six
```

Private Python uploads go directly to Gitea:

```sh
TWINE_REPOSITORY_URL="${GATEWAY_URL}/api/packages/${ARTEA_NAMESPACE}/pypi/" \
TWINE_USERNAME="${DEV1_USER}" \
TWINE_PASSWORD="${DEV1_TOKEN}" \
twine upload dist/*
```

See [clients-python.md](clients-python.md) for pip, uv, poetry, and `.pypirc`
configuration. Do not configure `extra-index-url` to pypi.org; that bypasses
Artea's private-name precedence guarantee.

## 5. Add real users

Bootstrap creates `dev1` for smoke tests and demos. For real users:

1. Sign in as `ARTEA_ADMIN_USER`.
2. Create the user in Gitea's admin UI, or connect Okta/OIDC later.
3. Add the user to the `${ARTEA_NAMESPACE}` org's `developers` team for package
   publish access.
4. Have the user create a PAT in **Settings -> Applications**.

Client PATs need the package scope plus `read:user` and `read:organization`:
`read:package` for install-only tokens, `write:package` to also publish. See
[publishing.md → Token scopes](publishing.md#token-scopes) for the full table
and the reasoning.

Use `username:PAT` for npm, pip, twine, uv, poetry, and curl. Do not use the
account password for package clients.

## 6. Production Kubernetes

`make dev` targets local Colima k3s; for a shared cluster:

```sh
make k8s-deploy
kubectl -n artea port-forward svc/artea-gateway 8080:80
```

The full flow — secrets, image digests, Ingress, upgrades — is in
[kubernetes.md](kubernetes.md). Production installs should set
`global.baseUrl` to the real HTTPS gateway URL and expose only the gateway
Ingress.

Before any non-throwaway deployment:

- Replace every placeholder secret.
- Keep Gitea, Verdaccio, devpi, and policy-sync private.
- Back up Gitea data; it is the store of record for users, PATs, policy, and
  private package artifacts.
- Decide whether users are manually admin-managed or created through Okta/OIDC.
- Run `make e2e`.

## Useful commands

```sh
make dev        # build images, deploy to local k3s, port-forward the gateway
make e2e        # smoke + S1-S20 against the cluster
make k8s-down   # uninstall the chart (PVCs survive)
kubectl -n artea logs -f deploy/artea-gateway   # follow a component's logs
```

To wipe everything, delete the namespace: `kubectl delete ns artea`. The
`artea-gitea-data` PVC is the store of record for users, PATs, policy, and
private package artifacts — back it up before deleting anything.

Next guides:

- [clients-npm.md](clients-npm.md)
- [clients-python.md](clients-python.md)
- [publishing.md](publishing.md)
- [operations.md](operations.md)
- [okta.md](okta.md)
