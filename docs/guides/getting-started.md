# Getting started

This guide gets a local Artea registry running, signs in with the bootstrap
accounts, and publishes one private package. Use it when you want the shortest
path from clone to a working npm/PyPI registry.

For Kubernetes, read this once for the product flow, then use
[kubernetes.md](kubernetes.md) for the cluster-specific install steps.

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

- Docker with Compose v2.
- `make`.
- `pnpm`, because the Verdaccio plugins are plain TypeScript packages.
- npm if you want to run the npm example.
- Python packaging tools if you want to run the Python publishing example.

## 1. Start the local stack

```sh
cp .env.example .env
make up
make bootstrap
make smoke
```

For local throwaway use, `.env.example` intentionally allows the `change-me-*`
placeholders. Before any shared or long-lived deployment, replace every
placeholder secret and set:

```sh
ARTEA_ALLOW_DEV_SECRETS=false
```

`make up` renders runtime configs, generates Gitea secret files, builds the
Verdaccio plugins, builds the local images, and starts the stack. `make
bootstrap` is idempotent; it creates the admin, namespace org, policy repo,
webhook, teams, demo user, and PATs.

Local bootstrap credentials are written here:

```sh
e2e/tmp/credentials.env
```

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
always-auth=true
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

Client PATs must include:

| Use | Required scopes |
|-----|-----------------|
| Install only | `read:package`, `read:user`, `read:organization` |
| Publish and install | `write:package`, `read:user`, `read:organization` |

Use `username:PAT` for npm, pip, twine, uv, poetry, and curl. Do not use the
account password for package clients.

## 6. Deploy beyond local Compose

For Kubernetes:

```sh
make k8s-deploy
kubectl -n artea port-forward svc/artea-gateway 8080:80
```

Local Kubernetes needs locally built Artea images; the full flow is in
[kubernetes.md](kubernetes.md). Production installs should set
`global.baseUrl` to the real HTTPS gateway URL and expose only the gateway
Ingress.

Before any non-throwaway deployment:

- Replace every placeholder secret.
- Keep Gitea, Verdaccio, devpi, and policy-sync private.
- Back up Gitea data; it is the store of record for users, PATs, policy, and
  private package artifacts.
- Decide whether users are manually admin-managed or created through Okta/OIDC.
- Run `make e2e` for Compose or `make k8s-e2e` for local Kubernetes.

## Useful commands

```sh
make logs       # follow all compose service logs
make down       # stop containers, keep all volumes
make clean      # wipe disposable caches, keep Gitea data
make backup     # cold backup of Gitea data
make destroy    # delete all local state, including users and packages
```

Next guides:

- [clients-npm.md](clients-npm.md)
- [clients-python.md](clients-python.md)
- [publishing.md](publishing.md)
- [operations.md](operations.md)
- [okta.md](okta.md)
