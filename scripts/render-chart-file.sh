#!/usr/bin/env bash
# Render one ConfigMap data file out of the Helm chart and write it to stdout —
# the compose/test path for configs that are single-sourced as Helm templates
# (the "Helm for both" model, docs/refactoring-plan.md Theme 2). Kubernetes
# consumes the same templates directly; rendering compose through Helm is what
# makes the two unable to drift.
#
#   render-chart-file.sh <show-only-template> <configmap-data-key> [extra helm --set flags...]
#
# Requires helm and yq on PATH. values-local.yaml supplies dev placeholders so
# the render succeeds offline without real image digests or secrets; none of
# those values reach the rendered file content.
set -euo pipefail
cd "$(dirname "$0")/.."

template=${1:?usage: render-chart-file.sh <template> <configmap-key> [--set ...]}
key=${2:?usage: render-chart-file.sh <template> <configmap-key> [--set ...]}
shift 2

for tool in helm yq; do
  command -v "$tool" >/dev/null || { echo "ERROR: $tool is required to render chart files" >&2; exit 1; }
done

# Helm validates Chart.yaml dependencies before rendering (even with
# --show-only), and deploy/helm/artea/charts/ is gitignored, so populate the
# pinned subcharts from the committed Chart.lock on a fresh checkout. Idempotent
# and offline once charts/ is populated; chatter goes to stderr so it never
# pollutes the rendered file on stdout.
if [ -z "$(ls -A deploy/helm/artea/charts 2>/dev/null || true)" ]; then
  echo "render-chart-file: fetching Helm chart dependencies (first run)..." >&2
  helm dependency build deploy/helm/artea >&2
fi

helm template artea deploy/helm/artea \
  --values deploy/helm/artea/values-local.yaml \
  --show-only "$template" "$@" \
  | yq "select(.kind == \"ConfigMap\") | .data[\"$key\"]"
