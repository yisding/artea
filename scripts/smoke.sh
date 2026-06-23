#!/usr/bin/env bash
# Artea smoke checks — fast gateway-level verification that the stack wiring
# works (subset of the S1-S20 e2e scenarios). Requires a bootstrapped cluster;
# uses the credentials bootstrap wrote.
#   BASE_URL          public gateway URL (beats the recorded GATEWAY_URL)
#   CREDENTIALS_FILE  credentials path (default e2e/tmp/credentials.env)
# The internal-only policy-sync /healthz probe is exec'd via kubectl.
set -euo pipefail
cd "$(dirname "$0")/.."

# shellcheck source=e2e/env.sh
source e2e/env.sh
resolve_credentials_file
[ -f "${CREDENTIALS_FILE}" ] || { echo "ERROR: ${CREDENTIALS_FILE} missing — run make bootstrap"; exit 1; }
load_credentials
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
# the gateway scope-routes the configured private npm scope under /npm/ to
# Gitea: an unknown private name 404s there, never in Verdaccio/npmjs
check "npm private scope routed to Gitea: unknown name 404s, never Verdaccio" 404 \
  "$(code -u "${DEV1_USER}:${DEV1_TOKEN}" "${GATEWAY_URL}/npm/@${ARTEA_NAMESPACE}%2fanything")"

# 3. pypi path: gateway auth guard + Gitea-404 fallthrough to devpi/pypi.org
check "pypi simple w/o auth gets Basic challenge" 401 "$(code "${GATEWAY_URL}/pypi/simple/six/")"
www=$(curl -s -o /dev/null -w '%{http_code} %header{www-authenticate}' "${GATEWAY_URL}/pypi/simple/six/")
check "401 carries WWW-Authenticate Basic" '401 Basic realm="Artea"' "${www}"
check "pypi simple for unpublished name falls through to public mirror" 200 \
  "$(code -u "${DEV1_USER}:${DEV1_TOKEN}" "${GATEWAY_URL}/pypi/simple/six/")"
# file links inside the fallthrough page must stay on the gateway origin via
# devpi's public mirror file route.
links=$(curl -s -u "${DEV1_USER}:${DEV1_TOKEN}" "${GATEWAY_URL}/pypi/simple/six/" \
  | grep -cF "href=\"${GATEWAY_URL}/root/pypi/" || true)
check "devpi simple page links route via gateway /root/" yes "$([ "${links}" -gt 0 ] && echo yes || echo no)"
check "raw devpi simple route is hidden" 404 \
  "$(code -u "${DEV1_USER}:${DEV1_TOKEN}" "${GATEWAY_URL}/root/pypi/+simple/six/")"
check "devpi file path w/o auth is denied" 401 \
  "$(code "${GATEWAY_URL}/root/pypi/+f/probe/file.whl")"

# 4. gitea package API direct paths (npm scope registry / pypi upload target)
check "gitea pypi simple 404s for unpublished name (auth'd)" 404 \
  "$(code -u "${DEV1_USER}:${DEV1_TOKEN}" "${GATEWAY_URL}/api/packages/${ARTEA_NAMESPACE}/pypi/simple/six/")"

# 5. policy-sync health (internal-only; via kubectl exec)
PS_HEALTH_PY="import json,urllib.request; d=json.load(urllib.request.urlopen('http://127.0.0.1:8920/healthz', timeout=3)); print('ok' if d.get('status')=='ok' and d.get('last_sync_ok') else 'bad')"
kc=(kubectl)
[ -n "${K8S_NAMESPACE}" ] && kc=(kubectl -n "${K8S_NAMESPACE}")
ps_health=$("${kc[@]}" exec "deploy/${K8S_POLICY_SYNC_DEPLOY}" -- python -c "${PS_HEALTH_PY}")
check "policy-sync /healthz reports synced" ok "${ps_health}"

echo
echo "smoke: ${pass} passed, ${fail} failed"
[ "${fail}" -eq 0 ]
