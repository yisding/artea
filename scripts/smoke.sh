#!/usr/bin/env bash
# Artea smoke checks — fast gateway-level verification that the stack wiring
# works (subset of the S1-S16 e2e scenarios). Requires a bootstrapped stack;
# uses the credentials bootstrap wrote. Portable across runtimes:
#   BASE_URL          public gateway URL (beats the recorded GATEWAY_URL)
#   CREDENTIALS_FILE  credentials path (default e2e/tmp/credentials.env)
#   RUNTIME           compose (default) | k8s — how the internal-only
#                     policy-sync healthz probe is exec'd
set -euo pipefail
cd "$(dirname "$0")/.."

RUNTIME="${RUNTIME:-compose}"
CREDENTIALS_FILE="${CREDENTIALS_FILE:-e2e/tmp/credentials.env}"
case "${CREDENTIALS_FILE}" in /*) ;; *) CREDENTIALS_FILE="./${CREDENTIALS_FILE}" ;; esac
[ -f "${CREDENTIALS_FILE}" ] || { echo "ERROR: ${CREDENTIALS_FILE} missing — run make bootstrap"; exit 1; }
# shellcheck disable=SC1090
source "${CREDENTIALS_FILE}"
# explicit BASE_URL beats the GATEWAY_URL recorded at bootstrap time
GATEWAY_URL="${BASE_URL:-${GATEWAY_URL:-http://localhost:8080}}"
# k8s runtime knobs (must match the chart; scripts/k8s-e2e.sh exports them)
K8S_NAMESPACE="${K8S_NAMESPACE:-}"
K8S_POLICY_SYNC_DEPLOY="${K8S_POLICY_SYNC_DEPLOY:-artea-policy-sync}"

pass=0; fail=0
check() { # <description> <expected> <actual>
  if [ "$2" = "$3" ]; then
    echo "ok   $1"
    pass=$((pass + 1))
  else
    echo "FAIL $1 (expected $2, got $3)"
    fail=$((fail + 1))
  fi
}
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

# 1. gateway liveness + Gitea UI through the gateway
check "gateway health endpoint" 200 "$(code "${GATEWAY_URL}/-/artea-gateway/health")"
check "gitea login page via gateway" 200 "$(code "${GATEWAY_URL}/user/login")"
check "anonymous / redirects to sign-in" 303 "$(code "${GATEWAY_URL}/")"

# 2. npm path (verdaccio behind /npm/): auth required, pull-through works
check "npm packument w/o auth is denied" 401 "$(code "${GATEWAY_URL}/npm/left-pad")"
check "npm packument with PAT (npmjs pull-through)" 200 \
  "$(code -u "${DEV1_USER}:${DEV1_TOKEN}" "${GATEWAY_URL}/npm/left-pad")"
check "npm @artea scope denied on verdaccio even with PAT" 403 \
  "$(code -u "${DEV1_USER}:${DEV1_TOKEN}" "${GATEWAY_URL}/npm/@artea%2fanything")"

# 3. pypi path: gateway auth guard + Gitea-404 fallthrough to devpi/pypi.org
check "pypi simple w/o auth gets Basic challenge" 401 "$(code "${GATEWAY_URL}/pypi/simple/six/")"
www=$(curl -s -o /dev/null -w '%{http_code} %header{www-authenticate}' "${GATEWAY_URL}/pypi/simple/six/")
check "401 carries WWW-Authenticate Basic" '401 Basic realm="Artea"' "${www}"
check "pypi simple for unpublished name falls through to devpi" 200 \
  "$(code -u "${DEV1_USER}:${DEV1_TOKEN}" "${GATEWAY_URL}/pypi/simple/six/")"
# file links inside the fallthrough page must stay on the gateway origin (/root/...)
links=$(curl -s -u "${DEV1_USER}:${DEV1_TOKEN}" "${GATEWAY_URL}/pypi/simple/six/" \
  | grep -c "href=\"${GATEWAY_URL}/root/pypi/" || true)
check "devpi simple page links route via gateway /root/" yes "$([ "${links}" -gt 0 ] && echo yes || echo no)"
check "devpi file path w/o auth is denied" 401 "$(code "${GATEWAY_URL}/root/pypi/")"

# 4. gitea package API direct paths (npm scope registry / pypi upload target)
check "gitea pypi simple 404s for unpublished name (auth'd)" 404 \
  "$(code -u "${DEV1_USER}:${DEV1_TOKEN}" "${GATEWAY_URL}/api/packages/artea/pypi/simple/six/")"

# 5. policy-sync health (internal-only; via docker/kubectl exec)
PS_HEALTH_PY="import json,urllib.request; d=json.load(urllib.request.urlopen('http://127.0.0.1:8920/healthz', timeout=3)); print('ok' if d.get('status')=='ok' and d.get('last_sync_ok') else 'bad')"
if [ "${RUNTIME}" = k8s ]; then
  kc=(kubectl)
  [ -n "${K8S_NAMESPACE}" ] && kc=(kubectl -n "${K8S_NAMESPACE}")
  ps_health=$("${kc[@]}" exec "deploy/${K8S_POLICY_SYNC_DEPLOY}" -- python -c "${PS_HEALTH_PY}")
else
  ps_health=$(docker compose exec -T policy-sync python -c "${PS_HEALTH_PY}")
fi
check "policy-sync /healthz reports synced" ok "${ps_health}"

echo
echo "smoke: ${pass} passed, ${fail} failed"
[ "${fail}" -eq 0 ]
