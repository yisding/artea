# Artea Helm chart (umbrella)

Kubernetes deployment of the Artea stack, conforming to `docs/ARCHITECTURE.md`
("Kubernetes deployment"). Upstream isolation (R7) extends to deployment
artifacts: Gitea and Verdaccio come from their **official charts**; our own
templates exist only for devpi, policy-sync, the gateway and the bootstrap
Job/RBAC.

Operator walkthrough (install, secrets, upgrades, local dev on colima):
`docs/guides/kubernetes.md`. This README documents the chart itself.

## Layout

| Piece | Source |
|-------|--------|
| Gitea | official chart `oci://docker.gitea.com/charts/gitea` (pinned in `Chart.yaml`), stock rootless image, app.ini translated into `gitea.config` + tpl'd `additionalConfigFromEnvs` (`ROOT_URL` from `global.baseUrl`), `custom/` template overlay via ConfigMap + `extraVolumes` |
| Verdaccio | official chart `https://charts.verdaccio.org` (pinned in `Chart.yaml`), stock image, our config via the chart's `configMap` value, plugins via init container (below) |
| devpi, policy-sync, gateway, bootstrap | our templates in `templates/` |

Run `helm dependency update` once after cloning (downloads the two subchart
tarballs into `charts/`; `Chart.lock` is committed).

## Fixed names (the K8s "Fixed contracts")

One release per namespace. Resource/Service names are deliberately
release-independent — they are cross-referenced from subchart values, the
verdaccio config and the gateway nginx.conf, and other tooling relies on them:

| Component | Service (cluster DNS) | Notes |
|-----------|----------------------|-------|
| gateway | `artea-gateway:80` | the only entrypoint; stateless, default 2 replicas |
| gitea | `artea-gitea-http:3000` | subchart `fullnameOverride: artea-gitea`; PVC `artea-gitea-data` is the only store of record |
| verdaccio | `artea-verdaccio:4873` | subchart `fullnameOverride`; PVC is a disposable cache |
| devpi | `artea-devpi:3141` | PVC `artea-devpi-data` is a disposable cache (fail-closed re-seed) |
| policy-sync | `artea-policy-sync:8920` | webhook receiver + `GET /policy/npm-rules.yaml` and `/policy/upstream-policy.yaml` |
| secrets | `artea-admin`, `artea-secrets`, `artea-policy-sync` | see below |
| bootstrap | Job/SA/Role/RoleBinding `artea-bootstrap` | post-install/post-upgrade hook |

Package namespace is separate from Kubernetes object naming:
`global.privateNamespace` controls the Gitea organization, npm scope, package
API owner, policy repo owner, and Gitea package-list landing page. It defaults
to `artea`; `secrets.adminUsername` defaults to
`<global.privateNamespace>-admin` when left empty.

## Verdaccio: official chart, with an init-container plugin delivery

The official chart is used (not the in-house fallback Deployment foreseen by
the architecture doc), because its values cover everything we need:

- `configMap` accepts our full adapted `config.yaml` verbatim;
- `extraInitContainers` (tpl-rendered) runs our assets image
  (`ghcr.io/yisding/artea-verdaccio-assets`, built by CI from
  `deploy/docker/verdaccio-assets/Dockerfile`) which copies the pre-built
  plugin workspace into an emptyDir;
- `persistence.volumes`/`persistence.mounts` mount that emptyDir read-only at
  `/verdaccio/plugins`, exactly like compose's bind mount.

The verdaccio *image* is stock (R7); only the plugin bits ride in via the init
container. K8s config differences vs `verdaccio/config.yaml` (compose):
`listen` omitted (chart-owned), and the filter plugin uses `policy_url` plus
`upstream_policy_url` against policy-sync instead of local `policy_file` paths —
policy is delivered over HTTP, there is no shared volume.

## Secrets

- `artea-admin` (`username`/`password`): Gitea bootstrap admin Secret. The
  Secret name is fixed; the `username` value defaults to
  `<global.privateNamespace>-admin` unless `secrets.adminUsername` is set.
  Consumed by the Gitea subchart (`gitea.admin.existingSecret`) and the
  bootstrap Job.
- `artea-secrets`: `DEV1_PASSWORD`, `DEVPI_ROOT_PASSWORD`,
  `POLICY_WEBHOOK_SECRET`.
- `artea-policy-sync`: `POLICY_SYNC_TOKEN` — owned by the bootstrap Job
  (`TOKEN_SINK=k8s-secret`): it mints a low-privilege `svc-policy` PAT, patches
  this Secret and rollout-restarts the policy-sync Deployment. `helm upgrade`
  preserves the in-cluster value (template `lookup`); on first install it holds
  `bootstrap-pending` and policy-sync idles until the hook completes.

All non-token values come from `.Values.secrets.*` (dev placeholders — always
override, e.g. `-f my-secrets.yaml`). Bring-your-own-Secret support is not
implemented yet; see the open items in `docs/guides/kubernetes.md`.

## Bootstrap Job env contract

Defined in `templates/bootstrap-job.yaml` (consumed by `scripts/bootstrap.sh`
k8s mode): `TOKEN_SINK=k8s-secret`, `SECRET_NAME=artea-policy-sync`,
`DEPLOYMENT_NAME=artea-policy-sync`, `GATEWAY_URL=http://artea-gateway`,
`GITEA_URL=http://artea-gitea-http:3000`,
`POLICY_SYNC_URL=http://artea-policy-sync:8920`,
`POLICY_SYNC_HOOK_URL=http://artea-policy-sync:8920/hooks/policy`, plus
`ARTEA_ADMIN_USER`, `ARTEA_ADMIN_PASSWORD`, `DEV1_PASSWORD`,
`POLICY_WEBHOOK_SECRET` from the Secrets above. The Job's Role allows exactly
get/patch on the `artea-policy-sync` Secret and Deployment.

## Single-node vs production

Defaults are single-node: plain `postgresql` + standalone `valkey` subcharts
(enabled), `replicas: 1` everywhere except the stateless gateway. Production
toggles (see the Gitea chart's HA docs): switch to `postgresql-ha` +
`valkey-cluster` (only one of each pair may be enabled), raise
`gitea.replicaCount` with RWX `gitea.persistence.accessModes`, and front the
gateway with the optional Ingress (`gateway.ingress.*`) — TLS/host routing
only, never the routing logic. Pin our images by `digest` (takes precedence
over `tag`).

## Files copied into the chart (keep in sync)

Helm cannot read outside the chart root, so three source files are duplicated
under `files/` and must be kept in sync with their originals:

| Chart copy | Original |
|------------|----------|
| `files/gateway/pep503.js` | `gateway/njs/pep503.js` |
| `files/gitea-templates/home.tmpl` | `gitea/custom/templates/home.tmpl.template` |
| `files/gitea-templates/base__head_navbar.tmpl` | `gitea/custom/templates/base/head_navbar.tmpl.template` |

Run `make check-chart-copies` before committing changes to any of these files.

`files/gateway/nginx.conf` is a Helm-templated *adaptation* of
`gateway/nginx.conf` (upstream blocks + cluster DNS instead of Docker's
resolver; see the header comment) — mirror any routing change made to the
compose config.

## Validation

```sh
helm dependency update deploy/helm/artea
helm lint deploy/helm/artea
helm template artea deploy/helm/artea > /dev/null
helm template artea deploy/helm/artea -f deploy/helm/artea/values-local.yaml > /dev/null
```
