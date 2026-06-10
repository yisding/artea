# Artea development targets. `make help` lists them.
SHELL := /bin/bash
COMPOSE := docker compose

.PHONY: help secrets plugins up down logs bootstrap smoke e2e clean

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

e2e: smoke ## scenario suite S1-S12 (requires up + bootstrap)
	./e2e/run.sh

clean: ## stop the stack and delete ALL state (volumes, e2e tmp files)
	$(COMPOSE) down -v --remove-orphans
	rm -rf e2e/tmp
