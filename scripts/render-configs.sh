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

render gateway/nginx.conf.template .generated/gateway/nginx.conf
render verdaccio/config.yaml.template .generated/verdaccio/config.yaml
render gitea/app.ini.template .generated/gitea/app.ini
render gitea/custom/templates/base/head_navbar.tmpl.template .generated/gitea/templates/base/head_navbar.tmpl
render gitea/custom/templates/home.tmpl.template .generated/gitea/templates/home.tmpl

echo "rendered configs for namespace '${ARTEA_NAMESPACE}'"
