#!/usr/bin/env bash
# Verify exact chart copies that Helm cannot read from outside chart root.
set -euo pipefail
cd "$(dirname "$0")/.."

check_copy() {
  local src=$1 dst=$2
  if cmp -s "$src" "$dst"; then
    echo "ok   $dst"
  else
    echo "FAIL $dst differs from $src" >&2
    return 1
  fi
}

check_copy gateway/njs/pep503.js deploy/helm/artea/files/gateway/pep503.js
check_copy gateway/njs/pep700.js deploy/helm/artea/files/gateway/pep700.js
check_copy gitea/custom/templates/home.tmpl.template deploy/helm/artea/files/gitea-templates/home.tmpl
check_copy gitea/custom/templates/base/head_navbar.tmpl.template deploy/helm/artea/files/gitea-templates/base__head_navbar.tmpl
