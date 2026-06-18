#!/usr/bin/env bash
# Render the single-source gateway nginx.conf for one deployment target and
# write it to stdout. The canonical source is the Helm template at
# deploy/helm/artea/files/gateway/nginx.conf; Kubernetes consumes it directly
# via the gateway ConfigMap, and this is the compose/test path that runs the
# same template through Helm so the two can never drift.
#
#   render-nginx.sh <upstreamMode> [privateNamespace]
#
# <upstreamMode> = compose | k8s; [privateNamespace] defaults to "artea".
# Requires helm and yq on PATH (the "Helm for both" model — Theme 2).
set -euo pipefail
cd "$(dirname "$0")/.."

mode=${1:?usage: render-nginx.sh <compose|k8s> [privateNamespace]}
ns=${2:-artea}

exec ./scripts/render-chart-file.sh templates/gateway.yaml nginx.conf \
  --set gateway.upstreamMode="$mode" \
  --set global.privateNamespace="$ns"
