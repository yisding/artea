# Local development on Colima k3s

Artea runs on Kubernetes only. For local development the contract is
[Colima](https://github.com/abiosoft/colima) with its built-in k3s: it ships a
Docker runtime whose image store the k3s node shares, so locally-built `docker
build` images are visible to the cluster with **no registry and no load step**.
The full production Helm flow is in [kubernetes.md](kubernetes.md); this guide is
the short local loop.

## Prerequisites

- `colima` (with the docker runtime, the default).
- `docker` CLI, `helm` ≥ 3.14, `kubectl`.
- `make`, `pnpm` (the Verdaccio plugins are TypeScript), plus npm / Python tools
  for the client examples.

## Start the cluster

```sh
colima start --kubernetes --cpu 4 --memory 8
```

`--kubernetes` enables k3s; the `--cpu 4 --memory 8` are recommended so the full
stack (Gitea + postgres + valkey + the four Artea services) has headroom. `make
dev` will start Colima for you if it is not already running, but it cannot resize
an already-running VM, so set the resources here on first start.

## Build, deploy, port-forward

```sh
make dev
```

`make dev` is turnkey:

1. ensures Colima k8s is up (starts it if not);
2. builds the four Artea images tagged `:local` — `devpi`, `policy-sync`,
   `bootstrap`, `verdaccio-assets` (the same images
   `.github/workflows/kind-e2e.yml` builds). With Colima's docker runtime the
   k3s node already sees them, so there is no `kind load` step;
3. `helm upgrade --install`s the chart with `values-local.yaml` (`:local` tags,
   `pullPolicy: Never`, dev-placeholder secrets). The bootstrap hook Job creates
   the admin, namespace org, policy repo, webhook, teams, demo `dev1` user and
   PATs, and waits for the first policy sync;
4. port-forwards the gateway to `http://localhost:8080` (the only public
   entrypoint; Gitea, Verdaccio, devpi and policy-sync stay internal).

`global.baseUrl` defaults to `http://localhost:8080` and must match how clients
reach the gateway — it drives Gitea's `ROOT_URL` and devpi's outside-url so
generated tarball/file URLs resolve back through the port-forward.

To rebuild a single image after a change and roll just that Deployment:

```sh
make images                                  # or one `docker build ...` from the Makefile
kubectl -n artea rollout restart deploy/artea-policy-sync
```

## Run the suite

In another shell (leave the port-forward running):

```sh
make e2e
```

`make e2e` (`scripts/k8s-e2e.sh`) extracts the credentials block from the
bootstrap Job logs into `e2e/tmp/credentials.env`, opens its own gateway
port-forward, and runs `scripts/smoke.sh` + `e2e/run.sh` against the cluster with
`RUNTIME=k8s`. Load the credentials in a shell with `source
e2e/tmp/credentials.env` to drive the registry by hand (see
[getting-started.md](getting-started.md)).

## Teardown

```sh
make k8s-down               # uninstall the chart; PVCs (incl. gitea-data) survive
kubectl delete ns artea     # full wipe, including the store of record
colima stop                 # or `colima delete` to reclaim the VM
```

`artea-gitea-data` is the only store of record (users, PATs, private packages);
the verdaccio/devpi PVCs are disposable caches. Deleting the namespace wipes
everything — a fresh `make dev` re-bootstraps from scratch.
