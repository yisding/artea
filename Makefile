# Artea development targets. `make help` lists them.
SHELL := /bin/bash
COMPOSE := docker compose
PROJECT := artea
# throwaway container for volume backup/restore (pinned tag, never latest)
UTIL_IMAGE ?= alpine:3.22
BACKUP_DIR := backups

.PHONY: help secrets plugins up down logs bootstrap smoke e2e clean destroy backup restore \
	k8s-deploy k8s-e2e k8s-down

# kubernetes flow (chart by deploy/helm/artea; see docs/ARCHITECTURE.md)
HELM_RELEASE ?= artea
HELM_CHART ?= deploy/helm/artea
K8S_NAMESPACE ?= artea
HELM_VALUES ?= deploy/helm/artea/values-local.yaml

help: ## list available targets
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "%-10s %s\n", $$1, $$2}'

.env:
	@echo "ERROR: .env is missing — cp .env.example .env and change the secrets"; exit 1

secrets: ## generate gitea/secrets/ (idempotent; required before first up)
	@./gitea/scripts/gen-secrets.sh

plugins: ## install + build the Verdaccio plugins (required before first up)
	cd verdaccio/plugins && pnpm install --frozen-lockfile && pnpm build

up: .env secrets ## build images and start the full stack, wait for health
	$(COMPOSE) up -d --build --wait --wait-timeout 300

down: ## stop the stack (volumes are preserved)
	$(COMPOSE) down

logs: ## follow logs of all services
	$(COMPOSE) logs -f --tail=100

bootstrap: .env ## idempotent S1: admin, org, policy repo + webhook, users, PATs
	./scripts/bootstrap.sh

smoke: ## gateway-level smoke checks (requires up + bootstrap)
	./scripts/smoke.sh

e2e: smoke ## scenario suite S1-S16 (requires up + bootstrap)
	./e2e/run.sh

k8s-deploy: ## helm install/upgrade the chart (bootstrap runs as a chart hook Job)
	helm upgrade --install $(HELM_RELEASE) $(HELM_CHART) \
		--namespace $(K8S_NAMESPACE) --create-namespace \
		$(if $(wildcard $(HELM_VALUES)),--values $(HELM_VALUES),) \
		--wait --timeout 10m

k8s-e2e: ## smoke + S1-S16 against the cluster (port-forward, RUNTIME=k8s)
	K8S_NAMESPACE=$(K8S_NAMESPACE) ./scripts/k8s-e2e.sh

k8s-down: ## uninstall the chart (PVCs survive; delete the namespace to wipe)
	helm uninstall $(HELM_RELEASE) --namespace $(K8S_NAMESPACE)

# clean wipes only what refills itself; gitea-data (users, private packages,
# PATs — the store of record) survives. Full wipe = `make destroy`.
clean: ## stop the stack and wipe the disposable caches (gitea-data preserved)
	$(COMPOSE) down --remove-orphans
	docker volume rm -f $(PROJECT)_devpi-data $(PROJECT)_verdaccio-storage $(PROJECT)_policy-data
	rm -rf e2e/tmp

destroy: ## DANGER: delete ALL state including gitea-data (interactive confirm)
	@echo "This permanently deletes ALL Artea state, including gitea-data"
	@echo "(users, private packages, PATs). 'make backup' first if in doubt."
	@read -r -p "Type the project name ($(PROJECT)) to confirm: " answer; \
		[ "$$answer" = "$(PROJECT)" ] || { echo "aborted"; exit 1; }
	$(COMPOSE) down -v --remove-orphans
	rm -rf e2e/tmp

backup: ## cold-backup gitea-data to ./backups/ (stops gitea briefly)
	@mkdir -p $(BACKUP_DIR)
	$(COMPOSE) stop gitea
	docker run --rm -v $(PROJECT)_gitea-data:/data:ro -v "$(CURDIR)/$(BACKUP_DIR)":/backup $(UTIL_IMAGE) \
		tar czf "/backup/gitea-data-$$(date +%Y%m%d-%H%M%S).tar.gz" -C /data .
	$(COMPOSE) start gitea
	@ls -t $(BACKUP_DIR)/gitea-data-*.tar.gz | head -1

restore: ## overwrite gitea-data from BACKUP=backups/gitea-data-<ts>.tar.gz
	@[ -n "$(BACKUP)" ] && [ -f "$(BACKUP)" ] || \
		{ echo "usage: make restore BACKUP=backups/gitea-data-<timestamp>.tar.gz"; exit 1; }
	@read -r -p "Overwrite gitea-data with $(BACKUP)? Type the project name ($(PROJECT)) to confirm: " answer; \
		[ "$$answer" = "$(PROJECT)" ] || { echo "aborted"; exit 1; }
	$(COMPOSE) stop gitea
	docker run --rm -v $(PROJECT)_gitea-data:/data -v "$(abspath $(BACKUP))":/backup.tar.gz:ro $(UTIL_IMAGE) \
		sh -c 'find /data -mindepth 1 -delete && tar xzf /backup.tar.gz -C /data'
	$(COMPOSE) start gitea
	@echo "restored $(BACKUP); caches refill on demand (slower first installs)"
