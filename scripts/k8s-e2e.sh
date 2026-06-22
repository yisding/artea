#!/usr/bin/env bash
# Artea k8s e2e driver (make k8s-e2e). The suite itself only knows BASE_URL;
# everything cluster-specific is wired up here:
#   1. extract the credentials block from the bootstrap hook Job's logs
#      (requires bootstrap.emitCredentials=true; falls back to a previously
#      extracted file when the Job is gone)
#   2. port-forward the gateway Service to localhost in the background
#   3. run scripts/smoke.sh + e2e/run.sh with BASE_URL
#   4. tear the port-forward down on exit
# Overridable: K8S_NAMESPACE, GATEWAY_SVC, BOOTSTRAP_JOB, LOCAL_PORT,
# CREDENTIALS_FILE, and the K8S_* names the suite uses. Defaults follow the
# chart's fixed "artea-<component>" naming (deploy/helm/artea/_helpers.tpl),
# which is independent of the Helm release name.
set -euo pipefail
cd "$(dirname "$0")/.."

K8S_NAMESPACE="${K8S_NAMESPACE:-artea}"
GATEWAY_SVC="${GATEWAY_SVC:-artea-gateway}"
BOOTSTRAP_JOB="${BOOTSTRAP_JOB:-artea-bootstrap}"
LOCAL_PORT="${LOCAL_PORT:-8080}"
CREDENTIALS_FILE="${CREDENTIALS_FILE:-e2e/tmp/credentials-k8s.env}"

log() { echo "[k8s-e2e] $*"; }
die() { echo "[k8s-e2e] ERROR: $*" >&2; exit 1; }
kc() { kubectl -n "${K8S_NAMESPACE}" "$@"; }

command -v kubectl >/dev/null || die "kubectl not found"

# ---- credentials: prefer the bootstrap Job logs, else a previous extraction ----
mkdir -p "$(dirname "${CREDENTIALS_FILE}")"
pod=$(kc get pods -l "job-name=${BOOTSTRAP_JOB}" --field-selector=status.phase=Succeeded \
  --sort-by=.metadata.creationTimestamp -o name 2>/dev/null | tail -1 || true)
block=""
if [ -n "${pod}" ]; then
  block=$(kc logs "${pod}" --tail=-1 2>/dev/null \
    | sed -n '/^----- BEGIN ARTEA CREDENTIALS -----$/,/^----- END ARTEA CREDENTIALS -----$/p' \
    | sed '1d;$d' || true)
fi
if [ -n "${block}" ]; then
  (umask 177; printf '%s\n' "${block}" > "${CREDENTIALS_FILE}")
  log "credentials extracted from ${pod} -> ${CREDENTIALS_FILE}"
elif [ -s "${CREDENTIALS_FILE}" ]; then
  log "no bootstrap Job logs (hook already cleaned up?); reusing ${CREDENTIALS_FILE}"
else
  die "no credentials: bootstrap logs did not contain an emitted credentials block and ${CREDENTIALS_FILE} is missing; deploy with bootstrap.emitCredentials=true for e2e/dev"
fi

# ---- port-forward the gateway (the only public entrypoint) ----------------------
# plain kubectl, NOT the kc() function: backgrounding a function makes $! the
# subshell pid, and the EXIT trap would kill the subshell but leak kubectl
kubectl -n "${K8S_NAMESPACE}" port-forward "svc/${GATEWAY_SVC}" "${LOCAL_PORT}:80" >/dev/null 2>&1 &
PF_PID=$!
trap 'kill "${PF_PID}" 2>/dev/null || true' EXIT
BASE_URL="http://localhost:${LOCAL_PORT}"
for i in $(seq 1 30); do
  curl -fsS --max-time 3 -o /dev/null "${BASE_URL}/-/artea-gateway/health" 2>/dev/null && break
  kill -0 "${PF_PID}" 2>/dev/null || die "port-forward to svc/${GATEWAY_SVC} exited — is the chart deployed? (make k8s-deploy)"
  [ "$i" -eq 30 ] && die "gateway not reachable via ${BASE_URL} after 30s"
  sleep 1
done
log "gateway reachable at ${BASE_URL} (port-forward pid ${PF_PID})"

# ---- run the suite against the cluster ------------------------------------------
export BASE_URL CREDENTIALS_FILE K8S_NAMESPACE
export K8S_POLICY_SYNC_DEPLOY="${K8S_POLICY_SYNC_DEPLOY:-artea-policy-sync}"
export K8S_DEVPI_DEPLOY="${K8S_DEVPI_DEPLOY:-artea-devpi}"
export K8S_DEVPI_PVC="${K8S_DEVPI_PVC:-artea-devpi-data}"
# webhook target as bootstrap wired it (cluster-internal service DNS)
export POLICY_SYNC_URL="${POLICY_SYNC_URL:-http://${K8S_POLICY_SYNC_DEPLOY}:8920}"

./scripts/smoke.sh
./e2e/run.sh
