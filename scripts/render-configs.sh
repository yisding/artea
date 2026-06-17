#!/usr/bin/env bash
# Render runtime configs from .env. The checked-in templates stay namespace-free;
# generated files under .generated/ are bind-mounted by compose.
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] || { echo "ERROR: .env is missing — cp .env.example .env and change the secrets" >&2; exit 1; }
set -a
# shellcheck disable=SC1091
source ./.env
set +a

ARTEA_NAMESPACE="${ARTEA_NAMESPACE:-artea}"

if ! [[ "${ARTEA_NAMESPACE}" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]]; then
  echo "ERROR: ARTEA_NAMESPACE must be a lowercase npm/Gitea-safe name: [a-z0-9]([a-z0-9-]*[a-z0-9])?" >&2
  exit 1
fi

render() {
  local src=$1 dst=$2
  mkdir -p "$(dirname "$dst")"
  sed "s|__ARTEA_NAMESPACE__|${ARTEA_NAMESPACE}|g" "$src" > "$dst"
}

# The gateway nginx.conf is single-sourced as a Helm template (no separate
# compose copy); render its compose variant through Helm so it can never drift
# from the Kubernetes copy. See scripts/render-nginx.sh.
mkdir -p .generated/gateway
./scripts/render-nginx.sh compose "${ARTEA_NAMESPACE}" > .generated/gateway/nginx.conf

# Verdaccio config is single-sourced as a Helm template as well; render its
# compose variant through Helm (configMode=compose) so it can't drift from k8s.
mkdir -p .generated/verdaccio
./scripts/render-chart-file.sh templates/verdaccio-config.yaml config.yaml \
  --set verdaccio.configMode=compose --set global.privateNamespace="${ARTEA_NAMESPACE}" \
  > .generated/verdaccio/config.yaml

render gitea/app.ini.template .generated/gitea/app.ini
render gitea/custom/templates/base/head_navbar.tmpl.template .generated/gitea/templates/base/head_navbar.tmpl
render gitea/custom/templates/home.tmpl.template .generated/gitea/templates/home.tmpl

echo "rendered configs for namespace '${ARTEA_NAMESPACE}'"
