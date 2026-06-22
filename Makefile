# Artea development targets. `make help` lists them.
SHELL := /bin/bash
PROJECT := artea

.PHONY: help plugins images dev e2e k8s-deploy k8s-e2e k8s-down

# kubernetes flow (chart by deploy/helm/artea; see docs/ARCHITECTURE.md)
HELM_RELEASE ?= artea
HELM_CHART ?= deploy/helm/artea
K8S_NAMESPACE ?= artea
HELM_VALUES ?= deploy/helm/artea/values-local.yaml
# local images consumed by values-local.yaml (tag :local, pullPolicy Never)
IMAGE_PREFIX ?= ghcr.io/yisding
IMAGE_TAG ?= local

help: ## list available targets
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "%-12s %s\n", $$1, $$2}'

plugins: ## install + build the Verdaccio plugins (required before building images)
	cd verdaccio/plugins && pnpm install --frozen-lockfile && pnpm build

# Build the four Artea service images for local k3s. The {name, context,
# dockerfile} triples mirror .github/workflows/kind-e2e.yml; with Colima's docker
# runtime the k3s node shares the docker image store, so no load step is needed.
# verdaccio-assets is special: its context is the *built* plugin workspace, so it
# depends on `plugins` above.
images: plugins ## build devpi, policy-sync, bootstrap and verdaccio-assets images (:local)
	docker build -t $(IMAGE_PREFIX)/artea-devpi:$(IMAGE_TAG) -f devpi/Dockerfile devpi
	docker build -t $(IMAGE_PREFIX)/artea-policy-sync:$(IMAGE_TAG) -f policy-sync/Dockerfile policy-sync
	docker build -t $(IMAGE_PREFIX)/artea-bootstrap:$(IMAGE_TAG) -f scripts/Dockerfile.bootstrap .
	docker build -t $(IMAGE_PREFIX)/artea-verdaccio-assets:$(IMAGE_TAG) \
		-f deploy/docker/verdaccio-assets/Dockerfile verdaccio/plugins

# Turnkey local dev on Colima's built-in k3s (docs/guides/local-dev.md): ensure
# the colima k8s context exists, build the images, deploy the chart, then
# port-forward the gateway. The context is pinned explicitly and `make dev` fails
# if it is missing, so a stray kubeconfig never aims the local-dev chart +
# placeholder secrets at a shared cluster. The bootstrap hook Job runs in k8s-deploy.
dev: ## turnkey local stack on Colima k3s: colima up + images + deploy + port-forward
	@if ! kubectl config get-contexts -o name 2>/dev/null | grep -qx colima; then \
		echo "No 'colima' kubectl context — starting Colima with Kubernetes (k3s)."; \
		echo "  (for a fuller stack: colima start --kubernetes --cpu 4 --memory 8)"; \
		colima start --kubernetes; \
	fi
	kubectl config use-context colima
	$(MAKE) images
	$(MAKE) k8s-deploy
	@echo "Stack deployed. Port-forwarding the gateway to http://localhost:8080"
	@echo "(Ctrl-C to stop; rerun 'make e2e' in another shell to drive the suite.)"
	kubectl -n $(K8S_NAMESPACE) port-forward svc/artea-gateway 8080:80

e2e: k8s-e2e ## smoke + S1-S20 against the cluster (alias for k8s-e2e)

k8s-deploy: ## helm install/upgrade the chart (bootstrap runs as a chart hook Job)
	# verdaccio dep is an https chart repo (the gitea dep is OCI, from Chart.lock);
	# register it so `helm dependency build` works on a fresh Helm home
	helm repo add verdaccio https://charts.verdaccio.org >/dev/null 2>&1 || true
	helm dependency build $(HELM_CHART)
	helm upgrade --install $(HELM_RELEASE) $(HELM_CHART) \
		--namespace $(K8S_NAMESPACE) --create-namespace \
		$(if $(wildcard $(HELM_VALUES)),--values $(HELM_VALUES),) \
		--wait --timeout 10m

k8s-e2e: ## smoke + S1-S20 against the cluster (via gateway port-forward)
	K8S_NAMESPACE=$(K8S_NAMESPACE) ./scripts/k8s-e2e.sh

k8s-down: ## uninstall the chart (PVCs survive; delete the namespace to wipe)
	helm uninstall $(HELM_RELEASE) --namespace $(K8S_NAMESPACE)
