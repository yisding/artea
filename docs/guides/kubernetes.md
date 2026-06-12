# Kubernetes: installing Artea with the Helm chart

The compose stack is the dev/reference deployment; Kubernetes is the
production shape (`docs/ARCHITECTURE.md`, "Kubernetes deployment"). The
umbrella chart lives at `deploy/helm/artea` — official Gitea + Verdaccio
charts as dependencies, our own templates only for devpi, policy-sync, the
gateway and the bootstrap Job. Chart-level details (fixed names, secrets
layout, the Verdaccio plugin delivery): `deploy/helm/artea/README.md`.

## Prerequisites

- `helm` ≥ 3.14 and `kubectl`
- a cluster. Local dev contract: [colima](https://github.com/abiosoft/colima)
  with its built-in k3s — `colima start --kubernetes`. colima's docker-runtime
  k3s shares the docker image store, so locally-built images are visible to
  the cluster without a registry.
- the four Artea images. CI builds and pushes
  `ghcr.io/yisding/artea-{devpi,policy-sync,bootstrap,verdaccio-assets}`; for
  local work build them yourself (next section).

## Install (local colima/k3s)

```sh
colima start --kubernetes

# our images, tagged :local (values-local.yaml uses pullPolicy: Never)
docker build -t ghcr.io/yisding/artea-devpi:local devpi/
docker build -t ghcr.io/yisding/artea-policy-sync:local policy-sync/
# bootstrap image (scripts/bootstrap.sh in k8s-secret mode) builds from the
# repo root: it needs scripts/ AND the policy/ seed files
docker build -t ghcr.io/yisding/artea-bootstrap:local -f scripts/Dockerfile.bootstrap .
pnpm -C verdaccio/plugins install && pnpm -C verdaccio/plugins build
docker build -t ghcr.io/yisding/artea-verdaccio-assets:local \
  -f deploy/docker/verdaccio-assets/Dockerfile verdaccio/plugins

helm dependency update deploy/helm/artea
helm install artea deploy/helm/artea -f deploy/helm/artea/values-local.yaml

kubectl logs -f job/artea-bootstrap   # S1: idempotent bootstrap, runs as a hook
```

The bootstrap Job waits for Gitea and the first successful policy sync; first
install on a fresh cluster also pulls postgres/valkey images, so give it a few
minutes (`bootstrap.activeDeadlineSeconds` defaults to 1200s).

## The port-forward contract

The gateway is the single public URL. Locally nothing is exposed; forward it:

```sh
kubectl port-forward svc/artea-gateway 8080:80
```

`global.baseUrl` (default `http://localhost:8080`) must match how clients
reach the gateway — it drives Gitea's `ROOT_URL` and devpi's outside-url, so
generated tarball/file URLs resolve back through the gateway. The e2e suite
only knows `BASE_URL`, so S1–S16 run unchanged against compose or K8s.

Client setup (`.npmrc`, pip index URL, PATs) is identical to compose:
`docs/guides/clients-npm.md`, `docs/guides/clients-python.md`.

## Secrets

Set real values for every key under `secrets:` (the defaults are dev
placeholders, mirroring `.env.example`). The private package namespace defaults
to `artea`; override `global.privateNamespace` to use another Gitea org / npm
scope. When `secrets.adminUsername` is omitted or empty, it defaults to
`<global.privateNamespace>-admin`.

```sh
cat > /tmp/artea-secrets.yaml <<'EOF'
global:
  privateNamespace: acme
secrets:
  adminPassword: ...
  dev1Password: ...
  devpiRootPassword: ...
  webhookSecret: ...
EOF
helm install artea deploy/helm/artea -f /tmp/artea-secrets.yaml
```

`POLICY_SYNC_TOKEN` is never supplied by you: the bootstrap Job mints the
low-privilege `svc-policy` PAT in Gitea and patches it into the
`artea-policy-sync` Secret (its Role allows get/patch on exactly that Secret
plus rollout-restart of the policy-sync Deployment). `helm upgrade` preserves
the in-cluster token; re-running bootstrap rotates it only when invalid or
over-privileged — the same idempotency contract as `make bootstrap` in
compose.

For production, expose the gateway via the optional Ingress (TLS + host
routing only — the routing logic stays in the gateway, by architecture rule):

```yaml
global:
  baseUrl: https://registry.example.com
gateway:
  ingress:
    enabled: true
    className: nginx
    host: registry.example.com
    tls:
      - secretName: artea-tls
        hosts: [registry.example.com]
```

## State

`artea-gitea-data` is the only store of record — back it up (same content as
the compose `gitea-data` volume; `docs/guides/operations.md`). The
verdaccio/devpi PVCs are disposable pull-through caches: deleting them is
safe; a fresh devpi volume serves nothing until policy-sync's next sync
(fail-closed), and Verdaccio re-fetches from npmjs on demand.

## Upgrades and upstream bumps (R7)

Same no-fork story as compose (`docs/guides/operations.md`), with the chart as
the pin location:

1. **Upstream chart bump**: edit the dependency version in
   `deploy/helm/artea/Chart.yaml`, run `helm dependency update`, review the
   subchart changelog for values changes, commit `Chart.lock`.
2. **Upstream image bump within a chart**: `gitea.image.tag` /
   `verdaccio.image.tag` in `values.yaml` (keep in sync with the compose pins
   in `.env` / `gitea/UPSTREAM`; re-verify the `files/gitea-templates/`
   overlay against the new Gitea templates — they are version-coupled).
3. **Our images**: CI pushes `:main` and digests; production values should pin
   `devpi.image.digest`, `policySync.image.digest`, `bootstrap.image.digest`,
   `verdaccio.pluginAssets.image.digest`.
4. Apply + verify:

```sh
helm upgrade artea deploy/helm/artea -f <your values>
# bootstrap re-runs automatically as a post-upgrade hook
BASE_URL=http://localhost:8080 make e2e   # with the port-forward running
```

Rollback: `helm rollback artea` — note that the bootstrap hook re-runs and the
Gitea database (postgres PVC) is not rolled back by Helm.

## Production sizing

Defaults are single-node (plain `postgresql` + standalone `valkey`,
`replicas: 1` except the gateway's 2). The HA toggles (`postgresql-ha`,
`valkey-cluster`, multiple Gitea replicas + RWX storage) are documented in
`deploy/helm/artea/README.md` and the upstream Gitea chart docs.

## Troubleshooting

- `kubectl logs job/artea-bootstrap` — bootstrap is idempotent; re-run it with
  `helm upgrade` (or delete the Job and `helm upgrade`) after fixing causes.
- policy-sync `/healthz` reports `last_sync_ok`; until bootstrap has minted
  its token it idles with failing syncs by design (fail-closed: Verdaccio
  rejects public fetches, devpi serves nothing).
- Gateway 502s right after install usually mean a backend isn't Ready yet;
  the gateway resolves Service names at startup and Services are stable, so
  no restart is needed once pods come up.
