#!/usr/bin/env bash
# Artea e2e scenario suite — codifies S1-S22 from docs/ARCHITECTURE.md (the
# definition of done for v1).
# Requires a deployed chart and a completed bootstrap (`make dev`/`make
# k8s-deploy` runs both as a hook Job); uses real client tools: npm with an
# isolated userconfig, pip/twine/build from a venv under e2e/tmp, git for the
# direct-push governance check (S14).
#
# The suite only knows BASE_URL; the few stack-mutating steps (S1 health, S15
# outage/wipe) reach the cluster via kubectl:
#   BASE_URL          public gateway URL (beats the recorded GATEWAY_URL)
#   CREDENTIALS_FILE  credentials path (default e2e/tmp/credentials.env)
#   E2E_SCENARIOS     optional comma/space list of prerequisite-aware scenario
#                     ids to run (for example: S1,S2,S15)
# `make e2e` (scripts/k8s-e2e.sh) wires all of this up for a cluster.
#
# Re-runnable: package versions are unique per run, fixed-version fixtures
# (tinynetrc 0.0.1) are deleted up front, and policy edits are reverted.
# Exit code is non-zero when any scenario fails; per-scenario PASS/FAIL with
# logs under e2e/tmp/run-<id>/logs/.
set -uo pipefail
umask 077
cd "$(dirname "$0")/.." || exit 1

# shellcheck disable=SC1091
source e2e/lib.sh
# shellcheck source=e2e/env.sh
source e2e/env.sh

for tool in curl jq npm pnpm python3 git kubectl; do
  command -v "$tool" >/dev/null || die "required tool '${tool}' not found"
done
# uv is provided by the suite venv (installed below alongside build/twine), so it
# is invoked as ${VENV}/bin/uv and is not part of the host-tool preflight above.

resolve_credentials_file
[ -f "${CREDENTIALS_FILE}" ] || die "${CREDENTIALS_FILE} missing — run 'make e2e' first"
load_credentials
GATEWAY_HOSTPORT="${GATEWAY_URL#http://}"
POLICY_REPO="${POLICY_REPO:-${ARTEA_NAMESPACE}/registry-policy}"
ARTEA_ADMIN_USER="${ARTEA_ADMIN_USER:-${ARTEA_NAMESPACE}-admin}"
# webhook target as seen by Gitea (S1 asserts the wiring bootstrap created);
# the cluster-internal policy-sync Service DNS. scripts/k8s-e2e.sh re-exports it.
POLICY_SYNC_URL="${POLICY_SYNC_URL:-http://artea-policy-sync:8920}"

# k8s runtime knobs (must match the chart; scripts/k8s-e2e.sh exports them)
K8S_NAMESPACE="${K8S_NAMESPACE:-}" # empty = kubectl context default
K8S_POLICY_SYNC_DEPLOY="${K8S_POLICY_SYNC_DEPLOY:-artea-policy-sync}"
K8S_DEVPI_DEPLOY="${K8S_DEVPI_DEPLOY:-artea-devpi}"
K8S_DEVPI_PVC="${K8S_DEVPI_PVC:-artea-devpi-data}"
# S15: the verdaccio filter plugin polls policy_url and only fails closed after
# a grace window of persistent failure (fail_grace_ms, default 60000); must
# match the chart's plugin config or S15 waits on the wrong clock
POLICY_GRACE_SECS="${POLICY_GRACE_SECS:-60}"

RUN_ID=$(date +%s)
ROOT=$(pwd)
# absolute paths: npm/pip run from fixture dirs, relative paths would break
WORK="${ROOT}/e2e/tmp/run-${RUN_ID}"
LOG_DIR="${WORK}/logs"
RESULT_DIR="${WORK}/results" # one file per scenario so parallel jobs can report
mkdir -p "${LOG_DIR}" "${RESULT_DIR}"
chmod 700 "${WORK}" "${LOG_DIR}" "${RESULT_DIR}"

# Per-run package versions keep the suite re-runnable without depending on
# cleanup having succeeded; cleanup deletes them anyway to avoid clutter.
NPM_SCOPE="@${ARTEA_NAMESPACE}"
NPM_NAME="${NPM_SCOPE}/hello-${ARTEA_NAMESPACE}"
NPM_NAME_ENC="%40${ARTEA_NAMESPACE}%2Fhello-${ARTEA_NAMESPACE}"
NPM_VERSION="0.0.${RUN_ID}"
NPM_RO_VERSION="0.1.${RUN_ID}" # plain semver: npm refuses prereleases without --tag
PY_NAME="${ARTEA_NAMESPACE}-hello"
PY_NAME_CASE="${ARTEA_NAMESPACE}-Hello"
PY_NAME_UNDERSCORE="${PY_NAME//-/_}"
PY_VERSION="0.0.${RUN_ID}"
PY_RO_VERSION="0.0.${RUN_ID}.post1"
SHADOW_NAME="tinynetrc" # real PyPI package, published privately as 0.0.1 in S9
SHADOW_VERSION="0.0.1"
# S21/S22: pnpm + uv publish round-trips; own per-run names so they never collide
# with the npm/twine fixtures above.
PNPM_NAME="${NPM_SCOPE}/pnpm-hello-${ARTEA_NAMESPACE}"
PNPM_NAME_ENC="%40${ARTEA_NAMESPACE}%2Fpnpm-hello-${ARTEA_NAMESPACE}"
PNPM_VERSION="0.0.${RUN_ID}"
UV_NAME="${ARTEA_NAMESPACE}-uv-hello"
UV_MODULE="${UV_NAME//-/_}"
UV_VERSION="0.0.${RUN_ID}"
# PEP 700 JSON Simple API media type (S20 upload-time enrichment)
PYPI_JSON_ACCEPT="application/vnd.pypi.simple.v1+json"

RO_TOKEN_NAME="e2e-ro-${RUN_ID}"
NO_PACKAGE_TOKEN_NAME="e2e-no-package-${RUN_ID}"
REVOKE_TOKEN_NAME="e2e-revoke-${RUN_ID}"
S14_TOKEN_NAME="e2e-s14-${RUN_ID}"

NPMRC="${WORK}/npmrc"
NPM_CACHE="${WORK}/npm-cache"
VENV="${ROOT}/e2e/tmp/venv"
VENV_PYTHON_STAMP="${VENV}/.artea-e2e-python"

# policy.toml fixture used by S5 and S13 to block left-pad 1.3.0 (each scenario
# authors it then reverts to ORIG_POLICY)
POLICY_BLOCK_LEFTPAD_130='# e2e fixture — blocks left-pad 1.3.0; reverted by the suite.
schema = 1

[defaults]
action = "allow"

[[rules]]
ecosystem = "npm"
name = "left-pad"
versions = "1.3.0"
action = "deny"
reason = "e2e fixture"'

POLICY_DIRTY=0       # policy.toml mutated by a scenario; cleanup reverts to ORIG_POLICY
POLICY_SYNC_SCALED=0 # S15: policy-sync scaled to 0 replicas
DEVPI_WIPED=0        # S15: devpi cache wiped, constraints not yet re-synced
E2E_SCENARIOS="${E2E_SCENARIOS:-}"
ALL_SCENARIOS="S1 S2 S3 S4 S5 S6 S7 S8 S9 S10 S11 S12 S13 S14 S15 S16 S17 S18 S19 S20 S21 S22 S23"

# ---- cleanup (idempotent, tolerates partial runs) ---------------------------------
cleanup() {
  local rc=$?
  set +e
  if [ "${POLICY_DIRTY}" = 1 ]; then
    put_policy_file policy.toml "${ORIG_POLICY}" "test(e2e): revert policy.toml (cleanup)" >/dev/null
  fi
  delete_pkg_version npm "${NPM_NAME_ENC}" "${NPM_VERSION}" >/dev/null
  delete_pkg_version npm "${NPM_NAME_ENC}" "${NPM_RO_VERSION}" >/dev/null
  delete_pkg_version pypi "${PY_NAME}" "${PY_VERSION}" >/dev/null
  delete_pkg_version pypi "${PY_NAME}" "${PY_RO_VERSION}" >/dev/null
  delete_pkg_version pypi "${SHADOW_NAME}" "${SHADOW_VERSION}" >/dev/null
  delete_pkg_version npm "${PNPM_NAME_ENC}" "${PNPM_VERSION}" >/dev/null
  delete_pkg_version pypi "${UV_NAME}" "${UV_VERSION}" >/dev/null
  delete_dev1_token "${RO_TOKEN_NAME}" >/dev/null
  delete_dev1_token "${NO_PACKAGE_TOKEN_NAME}" >/dev/null
  delete_dev1_token "${REVOKE_TOKEN_NAME}" >/dev/null
  delete_dev1_token "${S14_TOKEN_NAME}" >/dev/null
  # S15 partial-failure recovery: bring policy-sync back and make sure devpi
  # exists with real constraints again (startup sync of policy-sync)
  if [ "${POLICY_SYNC_SCALED}" = 1 ]; then
    kc scale "deployment/${K8S_POLICY_SYNC_DEPLOY}" --replicas=1 >/dev/null 2>&1
  fi
  if [ "${DEVPI_WIPED}" = 1 ]; then
    # PVC apply is idempotent; restart triggers policy-sync's startup sync
    [ -s "${WORK}/devpi-pvc.json" ] && kc apply -f "${WORK}/devpi-pvc.json" >/dev/null 2>&1
    kc scale "deployment/${K8S_DEVPI_DEPLOY}" --replicas=1 >/dev/null 2>&1
    kc rollout restart "deployment/${K8S_POLICY_SYNC_DEPLOY}" >/dev/null 2>&1
  fi
  exit "$rc"
}
trap cleanup EXIT

# ---- suite setup -------------------------------------------------------------------
log "run id ${RUN_ID}; work dir ${WORK}"

# The seeded policy.toml is default-allow (no deny rules) and is the suite's
# resting state. Scenarios mutate it and revert by writing ORIG_POLICY back.
ORIG_POLICY=$(get_policy_file policy.toml) || die "cannot read policy.toml from the policy repo"

write_npmrc "${NPMRC}" "${DEV1_TOKEN}"
mkdir -p "${NPM_CACHE}"

CURRENT_PYTHON=$(python3 -c 'import os, sys; print(os.path.realpath(sys.executable))') \
  || die "cannot resolve python3 path"
if [ ! -x "${VENV}/bin/python" ] \
  || [ ! -f "${VENV}/.artea-e2e-ready" ] \
  || [ "$(cat "${VENV_PYTHON_STAMP}" 2>/dev/null || true)" != "${CURRENT_PYTHON}" ]; then
  log "creating python venv with build/twine (one-time, network)"
  rm -rf "${VENV}"
  python3 -m venv "${VENV}" || die "venv creation failed"
  pip_env "${VENV}/bin/pip" install -q -U pip setuptools wheel build twine uv || die "venv tool install failed"
  printf '%s\n' "${CURRENT_PYTHON}" > "${VENV_PYTHON_STAMP}"
  touch "${VENV}/.artea-e2e-ready"
fi

index_url() { # <token> -> authenticated gateway simple index URL
  echo "http://dev1:$1@${GATEWAY_HOSTPORT}/pypi/simple/"
}
INDEX_URL=$(index_url "${DEV1_TOKEN}")

npm_fresh() { # npm with a throwaway cache: defeats packument 304-staleness
  local cache
  cache=$(mktemp -d "${WORK}/npm-fresh-XXXX")
  npm_config_userconfig="${NPMRC}" npm_config_cache="${cache}" npm "$@"
}

# ---- scenario harness ----------------------------------------------------------------
FAILED=0
SELECTED_SCENARIOS=0

scenario() { # <id> <description> <function> — run fn, record the result to a file
  # so serial and parallel (background-subshell) scenarios report identically; a
  # background job cannot update a parent variable, but it can write a file.
  local id=$1 desc=$2 fn=$3 t0 t1 status
  t0=$(date +%s)
  if "$fn" >"${LOG_DIR}/${id}.log" 2>&1; then status=PASS; else status=FAIL; fi
  t1=$(date +%s)
  # tab-separated (desc contains spaces); report() reads these in scenario order.
  printf '%s\t%s\t%s\n' "$status" "$((t1 - t0))" "$desc" > "${RESULT_DIR}/${id}"
}

report() { # print results in ALL_SCENARIOS order; set FAILED; tail failing logs
  local id status dur desc
  echo
  for id in ${ALL_SCENARIOS}; do
    [ -f "${RESULT_DIR}/${id}" ] || continue
    IFS=$'\t' read -r status dur desc < "${RESULT_DIR}/${id}"
    printf '%-4s %-4s %s (%ss)\n' "$id" "$status" "$desc" "$dur"
    if [ "$status" = FAIL ]; then
      FAILED=1
      sed 's/^/     | /' "${LOG_DIR}/${id}.log" | tail -25
    fi
  done
}

scenario_selected() { # <id>
  local id=$1 requested
  [ -z "${E2E_SCENARIOS}" ] && return 0
  requested=" ${E2E_SCENARIOS//,/ } "
  case "$requested" in
    *" ${id} "*) return 0 ;;
    *) return 1 ;;
  esac
}

run_scenario() { # <id> <description> <function> — runs if selected. The selected
  # count is computed up front in the run block (parallel jobs run in background
  # subshells that cannot update a parent counter).
  local id=$1 desc=$2 fn=$3
  scenario_selected "$id" && scenario "$id" "$desc" "$fn"
}

validate_scenario_selection() {
  local token requested=" ${E2E_SCENARIOS//,/ } " valid=" ${ALL_SCENARIOS} "
  [ -z "${E2E_SCENARIOS}" ] && return 0
  for token in $requested; do
    case "$valid" in
      *" ${token} "*) ;;
      *) die "unknown E2E_SCENARIOS id: ${token} (valid: ${ALL_SCENARIOS})" ;;
    esac
  done
}

# ---- S1: bootstrap state ----------------------------------------------------------------
s1_bootstrap() {
  local login
  # name-agnostic: every non-completed pod in the namespace must be Ready
  # (completed = the bootstrap hook Job); the gateway is probed end-to-end
  kc get pods -o json | jq -e '
    ([.items[] | select(.status.phase != "Succeeded")] | length) as $n
    | ([.items[] | select(.status.phase != "Succeeded")
        | (.status.conditions // [])[] | select(.type == "Ready" and .status == "True")]
       | length) == $n and $n > 0' >/dev/null \
    || { echo "not all pods are Ready:"; kc get pods; return 1; }
  echo "all pods Ready in namespace ${K8S_NAMESPACE:-<context default>}"
  [ "$(http_code "${GATEWAY_URL}/-/artea-gateway/health")" = 200 ] \
    || { echo "gateway health endpoint not reachable via ${GATEWAY_URL}"; return 1; }
  echo "gateway healthy via ${GATEWAY_URL}"
  admin_api GET /user
  [ "$API_CODE" = 200 ] || { echo "admin token rejected (HTTP ${API_CODE})"; return 1; }
  login=$(echo "$API_BODY" | jq -r .login)
  assert_eq "${ARTEA_ADMIN_USER}" "$login" "admin token belongs to ${login}" || return 1
  admin_api GET "/orgs/${ARTEA_NAMESPACE}"
  [ "$API_CODE" = 200 ] || { echo "org ${ARTEA_NAMESPACE} missing"; return 1; }
  [ "$(echo "$API_BODY" | jq -r .visibility)" = private ] || { echo "org ${ARTEA_NAMESPACE} is not private"; return 1; }
  admin_api GET "/repos/${POLICY_REPO}/contents/policy.toml"
  [ "$API_CODE" = 200 ] || { echo "policy.toml not seeded"; return 1; }
  admin_api GET "/repos/${POLICY_REPO}/hooks"
  [ "$API_CODE" = 200 ] || { echo "cannot list hooks"; return 1; }
  echo "$API_BODY" | jq -e --arg hook "${POLICY_SYNC_URL}/hooks/policy" \
    'any(.[]; .config.url == $hook and .active)' >/dev/null \
    || { echo "policy webhook not wired (expected ${POLICY_SYNC_URL}/hooks/policy)"; return 1; }
  login=$(curl -sf -H "Authorization: token ${DEV1_TOKEN}" "${GATEWAY_URL}/api/v1/user" | jq -r .login)
  assert_eq dev1 "$login" "dev1 PAT rejected" || return 1
  echo "org, policy repo, webhook, admin+dev1 PATs all present"
}

# ---- S2: npm publish private scoped package -> 201 in Gitea ------------------------------
s2_npm_publish() {
  make_npm_pkg "${WORK}/hello-${ARTEA_NAMESPACE}" "${NPM_NAME}" "${NPM_VERSION}"
  (cd "${WORK}/hello-${ARTEA_NAMESPACE}" && npm_e2e publish --loglevel=http) || { echo "npm publish failed"; return 1; }
  pkg_version_exists npm "${NPM_NAME_ENC}" "${NPM_VERSION}" \
    || { echo "Gitea does not list ${NPM_NAME}@${NPM_VERSION} after publish"; return 1; }
  echo "Gitea package API confirms ${NPM_NAME}@${NPM_VERSION}"
}

# ---- S3: npm install private scoped package resolves from Gitea --------------------------
s3_npm_install_private() {
  local proj="${WORK}/proj-s3" resolved version
  mkdir -p "$proj"
  echo '{"name":"e2e-consumer-s3","version":"1.0.0"}' > "$proj/package.json"
  (cd "$proj" && npm_e2e install "${NPM_NAME}@${NPM_VERSION}") || { echo "npm install failed"; return 1; }
  version=$(jq -r .version "$proj/node_modules/${NPM_NAME}/package.json")
  assert_eq "${NPM_VERSION}" "$version" "installed version mismatch" || return 1
  resolved=$(jq -r ".packages[\"node_modules/${NPM_NAME}\"].resolved" "$proj/package-lock.json")
  echo "resolved: ${resolved}"
  # even under gateway scope routing (packument fetched via /npm/) the tarball
  # URL is Gitea-built from ROOT_URL, so it stays on /api/packages/<namespace>/npm/
  assert_origin gitea-npm "$resolved" "tarball did not come from Gitea (gateway scope routing)" || return 1
}

# ---- S4: npm install left-pad via Verdaccio pull-through ----------------------------------
s4_npm_install_public() {
  local proj="${WORK}/proj-s4" resolved
  # k8s HTTP-mode startup race: the Verdaccio filter fails closed (empty
  # packument) until its first policy poll lands, so wait until public
  # pull-through is live before installing. With the default-allow seed policy,
  # left-pad 1.3.0 being visible == the filter has fetched the policy.
  wait_for 60 2 "left-pad visible via Verdaccio pull-through (filter has policy)" left_pad_130_visible || return 1
  mkdir -p "$proj"
  echo '{"name":"e2e-consumer-s4","version":"1.0.0"}' > "$proj/package.json"
  (cd "$proj" && npm_e2e install left-pad@1.3.0) || { echo "npm install left-pad failed"; return 1; }
  [ -f "$proj/node_modules/left-pad/package.json" ] || { echo "left-pad not in node_modules"; return 1; }
  resolved=$(jq -r '.packages["node_modules/left-pad"].resolved' "$proj/package-lock.json")
  echo "resolved: ${resolved}"
  assert_origin gateway "$resolved" "tarball did not come through the gateway /npm/ (verdaccio) path" || return 1
}

# ---- S5: block left-pad 1.3.0 via policy.toml push ----------------------------------------
left_pad_130_hidden() {
  local body
  body=$(gw_get /npm/left-pad) || return 1
  ! grep -q '"1.3.0":' <<<"$body"
}
left_pad_130_visible() {
  local body
  body=$(gw_get /npm/left-pad) || return 1
  grep -q '"1.3.0":' <<<"$body"
}

s5_npm_policy_block() {
  local versions
  # establish the visible baseline first (see S4): a fail-closed empty packument
  # would otherwise satisfy the hidden-check below for the wrong reason, masking
  # whether the block actually took effect.
  wait_for 60 2 "left-pad 1.3.0 visible before applying the block" left_pad_130_visible || return 1
  POLICY_DIRTY=1
  put_policy_file policy.toml "${POLICY_BLOCK_LEFTPAD_130}" "test(e2e): S5 block left-pad 1.3.0" || return 1
  wait_for 45 2 "left-pad 1.3.0 filtered from packument" left_pad_130_hidden || return 1
  versions=$(npm_fresh view left-pad versions --json) || { echo "npm view failed"; return 1; }
  echo "npm view left-pad versions: ${versions}"
  echo "$versions" | jq -e 'length > 0' >/dev/null || { echo "empty versions list"; return 1; }
  echo "$versions" | jq -e 'index("1.3.0") == null' >/dev/null \
    || { echo "1.3.0 still present in npm view output"; return 1; }
  put_policy_file policy.toml "${ORIG_POLICY}" "test(e2e): S5 revert policy" || return 1
  wait_for 45 2 "left-pad 1.3.0 visible again after revert" left_pad_130_visible || return 1
  POLICY_DIRTY=0
}

# ---- S6: twine upload private wheel -> Gitea ---------------------------------------------
s6_twine_upload() {
  make_py_pkg "${WORK}/${PY_NAME}" "${PY_NAME}" "${PY_VERSION}"
  build_wheel "${WORK}/${PY_NAME}" || { echo "wheel build failed"; return 1; }
  twine_upload "${DEV1_TOKEN}" "${WORK}/${PY_NAME}/dist/"*.whl || { echo "twine upload failed"; return 1; }
  pkg_version_exists pypi "${PY_NAME}" "${PY_VERSION}" \
    || { echo "Gitea does not list ${PY_NAME} ${PY_VERSION} after upload"; return 1; }
  echo "Gitea package API confirms ${PY_NAME} ${PY_VERSION} (artifact stored in Gitea)"
}

# ---- S7: pip install private wheel via the gateway index ----------------------------------
s7_pip_install_private() {
  local report="${WORK}/s7-report.json" url
  pip_e2e install -q --index-url "${INDEX_URL}" --force-reinstall --no-deps \
    --report "$report" "${PY_NAME}==${PY_VERSION}" || { echo "pip install failed"; return 1; }
  url=$(jq -r '.install[0].download_info.url' "$report")
  echo "downloaded from: ${url}"
  assert_origin gitea-pypi "$url" "wheel did not come from Gitea" || return 1
}

# ---- S8: pip install six via gateway -> devpi -> PyPI ----------------------------------------
s8_pip_install_public() {
  local report="${WORK}/s8-report.json" url
  pip_e2e install -q --index-url "${INDEX_URL}" --force-reinstall --no-deps \
    --report "$report" six || { echo "pip install six failed"; return 1; }
  url=$(jq -r '.install[0].download_info.url' "$report")
  echo "downloaded from: ${url}"
  # public PyPI mirror path through the gateway
  assert_origin devpi "$url" "six did not come through the devpi pull-through path" || return 1
}

# ---- S9: private name shadows the public one entirely ----------------------------------------
s9_precedence_shadowing() {
  local out line
  # fixed version 0.0.1 per the scenario; pre-delete so re-runs are clean
  delete_pkg_version pypi "${SHADOW_NAME}" "${SHADOW_VERSION}" || return 1
  make_py_pkg "${WORK}/${SHADOW_NAME}" "${SHADOW_NAME}" "${SHADOW_VERSION}"
  build_wheel "${WORK}/${SHADOW_NAME}" || { echo "wheel build failed"; return 1; }
  twine_upload "${DEV1_TOKEN}" "${WORK}/${SHADOW_NAME}/dist/"*.whl || { echo "twine upload failed"; return 1; }
  out=$(pip_e2e index versions "${SHADOW_NAME}" --index-url "${INDEX_URL}" 2>&1) \
    || { echo "pip index versions failed: ${out}"; return 1; }
  echo "$out"
  line=$(echo "$out" | grep '^Available versions:') || { echo "no versions line"; return 1; }
  # the public package has 1.x releases; the gateway must show ONLY 0.0.1
  [ "$line" = "Available versions: ${SHADOW_VERSION}" ] \
    || { echo "expected ONLY ${SHADOW_VERSION}; public versions leaked through"; return 1; }
}

# ---- S10: policy.toml constrains pypi urllib3 to <2 -------------------------------------------
urllib3_v2_hidden() {
  local body
  body=$(gw_get /pypi/simple/urllib3/) || return 1
  ! grep -q 'urllib3-2\.' <<<"$body"
}
urllib3_v2_visible() {
  local body
  body=$(gw_get /pypi/simple/urllib3/) || return 1
  grep -q 'urllib3-2\.' <<<"$body"
}

s10_pypi_policy_constraint() {
  local out line wheel body blocked_file_path
  body=$(gw_get /pypi/simple/urllib3/) \
    || { echo "pre-policy urllib3 simple fetch failed"; return 1; }
  blocked_file_path=$(echo "$body" \
    | grep -Eo 'href="[^"]*urllib3-2[^"]*\.(whl|tar\.gz|zip)[^"]*"' \
    | head -1 \
    | sed -E 's/^href="//; s/"$//; s#^https?://[^/]+##; s/#.*$//')
  [ -n "$blocked_file_path" ] \
    || { echo "could not find a urllib3 2.x file link before applying constraints"; return 1; }
  POLICY_DIRTY=1
  put_policy_file policy.toml "$(cat <<'TOML'
# e2e fixture (S10) — policy.toml constraining urllib3 to <2; reverted by the suite.
schema = 1

[defaults]
action = "allow"

[[rules]]
ecosystem = "pypi"
name = "urllib3"
versions = ">=2"
action = "deny"
reason = "e2e fixture (S10): pin to 1.x"
TOML
)" "test(e2e): S10 constrain urllib3<2 via policy.toml" || return 1
  wait_for 45 2 "urllib3 2.x filtered from simple index" urllib3_v2_hidden || return 1
  for path in "/root/pypi/+simple/urllib3/" "/root/constrained/+simple/urllib3/"; do
    assert_code 404 -u "dev1:${DEV1_TOKEN}" "${GATEWAY_URL}${path}" || return 1
  done
  echo "direct devpi simple routes are not reachable through the gateway"
  assert_code 403 -u "dev1:${DEV1_TOKEN}" "${GATEWAY_URL}${blocked_file_path}" || return 1
  echo "direct devpi file URLs obey current PyPI constraints"
  out=$(pip_e2e index versions urllib3 --index-url "${INDEX_URL}" 2>&1) \
    || { echo "pip index versions failed: ${out}"; return 1; }
  line=$(echo "$out" | grep '^Available versions:') || { echo "no versions line"; return 1; }
  echo "$line"
  grep -Eq '(:|, )2\.' <<<"$line" && { echo "a 2.x version is still visible"; return 1; }
  # prove pip actually resolves <2: download latest allowed wheel via the gateway
  rm -rf "${WORK}/s10-dl" && mkdir -p "${WORK}/s10-dl"
  pip_e2e download -q --index-url "${INDEX_URL}" --no-deps -d "${WORK}/s10-dl" urllib3 \
    || { echo "pip download urllib3 failed"; return 1; }
  wheel=$(ls "${WORK}/s10-dl" | head -1)
  echo "pip resolved: ${wheel}"
  case "$wheel" in
    urllib3-1.*) ;;
    *) echo "pip resolved a non-1.x urllib3"; return 1 ;;
  esac
  put_policy_file policy.toml "${ORIG_POLICY}" "test(e2e): S10 revert policy" || return 1
  wait_for 45 2 "urllib3 2.x visible again after revert" urllib3_v2_visible || return 1
  POLICY_DIRTY=0
}

# ---- S11: one PAT everywhere; read:package can pull but not publish (401) ---------------------
s11_token_scopes() {
  local ro_token no_package_token out proj="${WORK}/proj-s11"
  # dev1's single write:package PAT already drove S2-S10 (publish+install, npm+pypi)
  ro_token=$(mint_dev1_token "${RO_TOKEN_NAME}" '["read:package","read:user","read:organization"]') \
    || { echo "minting read-only token failed"; return 1; }

  # pulls succeed with the read-only token: private npm, private pypi, public npm
  write_npmrc "${WORK}/npmrc-ro" "${ro_token}"
  mkdir -p "$proj"
  echo '{"name":"e2e-consumer-s11","version":"1.0.0"}' > "$proj/package.json"
  (cd "$proj" && npm_config_userconfig="${WORK}/npmrc-ro" npm_config_cache="${WORK}/npm-cache-ro" \
    npm install "${NPM_NAME}@${NPM_VERSION}") || { echo "read-only npm install failed"; return 1; }
  pip_e2e install -q --index-url "$(index_url "${ro_token}")" --force-reinstall --no-deps \
    "${PY_NAME}==${PY_VERSION}" || { echo "read-only pip install failed"; return 1; }
  assert_code 200 -u "dev1:${ro_token}" "${GATEWAY_URL}/npm/left-pad" || return 1
  echo "read-only token installs fine (npm private+public, pip private)"

  # npm publish with the read-only token must be rejected with 401 (not 403)
  make_npm_pkg "${WORK}/hello-${ARTEA_NAMESPACE}-ro" "${NPM_NAME}" "${NPM_RO_VERSION}"
  out=$( (cd "${WORK}/hello-${ARTEA_NAMESPACE}-ro" && npm_config_userconfig="${WORK}/npmrc-ro" \
    npm_config_cache="${WORK}/npm-cache-ro" npm publish) 2>&1) && {
    echo "npm publish with read-only token unexpectedly succeeded"; return 1; }
  cli_status_is "$out" 401 || { echo "npm publish rejection was not 401:"; echo "$out"; return 1; }
  cli_status_is "$out" 403 && { echo "got 403, expected 401:"; echo "$out"; return 1; }
  pkg_version_exists npm "${NPM_NAME_ENC}" "${NPM_RO_VERSION}" && { echo "package was created anyway"; return 1; }
  echo "npm publish with read-only token rejected with 401"

  # twine upload with the read-only token must be rejected with 401 (not 403)
  make_py_pkg "${WORK}/${PY_NAME}-ro" "${PY_NAME}" "${PY_RO_VERSION}"
  build_wheel "${WORK}/${PY_NAME}-ro" || { echo "wheel build failed"; return 1; }
  out=$(twine_upload "${ro_token}" "${WORK}/${PY_NAME}-ro/dist/"*.whl 2>&1) && {
    echo "twine upload with read-only token unexpectedly succeeded"; return 1; }
  cli_status_is "$out" 401 || { echo "twine rejection was not 401:"; echo "$out"; return 1; }
  cli_status_is "$out" 403 && { echo "got 403, expected 401:"; echo "$out"; return 1; }
  pkg_version_exists pypi "${PY_NAME}" "${PY_RO_VERSION}" && { echo "package was created anyway"; return 1; }
  echo "twine upload with read-only token rejected with 401"

  no_package_token=$(mint_dev1_token "${NO_PACKAGE_TOKEN_NAME}" '["read:user","read:organization"]') \
    || { echo "minting no-package-scope token failed"; return 1; }
  for url in \
    "${GATEWAY_URL}/npm/left-pad" \
    "${GATEWAY_URL}/npm/${NPM_NAME_ENC}" \
    "${GATEWAY_URL}/pypi/simple/six/" \
    "${GATEWAY_URL}/api/packages/${ARTEA_NAMESPACE}/pypi/simple/${PY_NAME}/"
  do
    assert_code 403 -u "dev1:${no_package_token}" "$url" || return 1
  done
  assert_code 403 -u "dev1:${no_package_token}" \
    "${GATEWAY_URL}/api/v1/packages/${ARTEA_NAMESPACE}/?type=pypi&limit=1" \
    || { echo "Gitea package-management probe with no package scope rejected"; return 1; }
  echo "token with read:user/read:organization but no package scope is rejected before package proxies"

  delete_dev1_token "${RO_TOKEN_NAME}"
  delete_dev1_token "${NO_PACKAGE_TOKEN_NAME}"
}

# ---- S12: PAT revocation takes effect within 60s ----------------------------------------------
s12_revocation() {
  local token npm_code pypi_code revoked_at elapsed
  token=$(mint_dev1_token "${REVOKE_TOKEN_NAME}" '["read:package","read:user","read:organization"]') \
    || { echo "minting revocation token failed"; return 1; }

  # prime both auth caches (gateway auth_request cache + verdaccio auth cache)
  npm_code=$(http_code -u "dev1:${token}" "${GATEWAY_URL}/npm/left-pad")
  pypi_code=$(http_code -u "dev1:${token}" "${GATEWAY_URL}/pypi/simple/six/")
  [ "$npm_code" = 200 ] && [ "$pypi_code" = 200 ] \
    || { echo "pre-revocation installs broken (npm ${npm_code}, pypi ${pypi_code})"; return 1; }
  echo "pre-revocation: npm 200, pypi 200"

  delete_dev1_token "${REVOKE_TOKEN_NAME}" || { echo "token deletion failed"; return 1; }
  revoked_at=$(date +%s)

  while true; do
    npm_code=$(http_code -u "dev1:${token}" "${GATEWAY_URL}/npm/left-pad")
    pypi_code=$(http_code -u "dev1:${token}" "${GATEWAY_URL}/pypi/simple/six/")
    elapsed=$(( $(date +%s) - revoked_at ))
    if [ "$npm_code" = 401 ] && [ "$pypi_code" = 401 ]; then
      echo "both paths reject the revoked PAT after ${elapsed}s (npm ${npm_code}, pypi ${pypi_code})"
      break
    fi
    if [ "$elapsed" -gt 90 ]; then
      echo "still accepted after ${elapsed}s (npm ${npm_code}, pypi ${pypi_code})"
      return 1
    fi
    sleep 1
  done
  [ "$elapsed" -le 60 ] || { echo "revocation took ${elapsed}s, budget is 60s"; return 1; }
}

# ---- S13: tarball enforcement/redirects (+ anonymous /npm/ service endpoints) -----------------
TARBALL_BLOCKED="${GATEWAY_URL}/npm/left-pad/-/left-pad-1.3.0.tgz"
TARBALL_ALLOWED="${GATEWAY_URL}/npm/left-pad/-/left-pad-1.2.0.tgz"

tarball_130_blocked() { [ "$(http_code -u "dev1:${DEV1_TOKEN}" "${TARBALL_BLOCKED}")" = 403 ]; }
tarball_130_allowed() { [ "$(http_code -u "dev1:${DEV1_TOKEN}" "${TARBALL_BLOCKED}")" = 302 ]; }
assert_public_tarball_redirect() { # <url> <expected-upstream-url>
  local actual
  actual=$(curl -s -o /dev/null -w '%{http_code} %header{location}' -u "dev1:${DEV1_TOKEN}" "$1")
  assert_eq "302 $2" "$actual" "policy-cleared public tarball should redirect to npmjs" || return 1
}

s13_tarball_enforcement() {
  local body
  # anonymous /npm/: Verdaccio's service endpoints must challenge, not answer
  body=$(curl -s -o /dev/null -w '%{http_code} %header{www-authenticate}' "${GATEWAY_URL}/npm/-/ping")
  assert_eq '401 Basic realm="Artea"' "$body" "anonymous /npm/-/ping: expected 401 + Basic challenge" || return 1
  assert_code 401 "${GATEWAY_URL}/npm/-/v1/search?text=left-pad" || return 1
  echo "anonymous /npm/-/ping and /npm/-/v1/search get 401 with Basic challenge"

  POLICY_DIRTY=1
  put_policy_file policy.toml "${POLICY_BLOCK_LEFTPAD_130}" "test(e2e): S13 block left-pad 1.3.0" || return 1
  wait_for 45 2 "blocked tarball rejected with 403" tarball_130_blocked || return 1
  body=$(curl -s -u "dev1:${DEV1_TOKEN}" "${TARBALL_BLOCKED}")
  assert_contains 'blocked by registry policy' "$body" \
    "403 body is not the policy middleware's JSON error: ${body}" || return 1
  assert_public_tarball_redirect "${TARBALL_ALLOWED}" "https://registry.npmjs.org/left-pad/-/left-pad-1.2.0.tgz" || return 1
  echo "blocked 1.3.0 tarball -> 403 (policy JSON); unblocked 1.2.0 tarball -> 302 npmjs redirect"
  put_policy_file policy.toml "${ORIG_POLICY}" "test(e2e): S13 revert policy" || return 1
  wait_for 45 2 "tarball redirects again after revert" tarball_130_allowed || return 1
  POLICY_DIRTY=0
}

# ---- S14: branch protection on registry-policy@main (governance) ------------------------------
s14_branch_protection() {
  local token auth_header clone="${WORK}/s14-policy-clone" out sha b64
  token=$(mint_dev1_token "${S14_TOKEN_NAME}" '["write:repository","read:user"]') \
    || { echo "minting dev1 repo-scoped token failed"; return 1; }
  auth_header=$(printf 'dev1:%s' "$token" | json_b64)

  # a real git push to main as dev1 must hit the protected-branch pre-receive hook
  git -c "http.extraHeader=Authorization: Basic ${auth_header}" \
    clone -q "http://${GATEWAY_HOSTPORT}/${POLICY_REPO}.git" "$clone" \
    || { echo "git clone as dev1 failed (developers team should have code read)"; return 1; }
  (cd "$clone" && echo "# e2e S14 direct-push probe" >> policy.toml \
    && git -c user.email=dev1@localhost -c user.name=dev1 commit -aqm "test(e2e): S14 probe") || return 1
  if out=$(git -C "$clone" -c "http.extraHeader=Authorization: Basic ${auth_header}" \
    push origin HEAD:main 2>&1); then
    echo "direct push to main as dev1 unexpectedly succeeded"; echo "$out"; return 1
  fi
  grep -qi 'protected branch' <<<"$out" \
    || { echo "push failed, but not with the protected-branch rejection:"; echo "$out"; return 1; }
  echo "dev1 git push to main rejected: protected branch"

  # the contents API goes through the same check: dev1 gets 403
  admin_api GET "/repos/${POLICY_REPO}/contents/policy.toml"
  sha=$(echo "$API_BODY" | jq -r .sha)
  b64=$(printf '%s\n' '# e2e S14 contents-API probe' | json_b64)
  assert_code 403 -X PUT -H "Authorization: token ${token}" -H 'Content-Type: application/json' \
    -d "{\"content\":\"${b64}\",\"sha\":\"${sha}\",\"message\":\"test(e2e): S14 contents probe\"}" \
    "${GATEWAY_URL}/api/v1/repos/${POLICY_REPO}/contents/policy.toml" \
    || { echo "dev1 contents-API edit was not rejected with 403"; return 1; }
  [ "$(get_policy_file policy.toml)" = "${ORIG_POLICY}" ] \
    || { echo "policy.toml on main changed despite the rejections"; return 1; }
  echo "dev1 contents-API edit rejected with 403; main unchanged"

  # the configured admin stays on the push allowlist: contents-API edits still land
  POLICY_DIRTY=1
  put_policy_file policy.toml "${ORIG_POLICY}"$'\n'"# e2e S14: admin allowlist probe" \
    "test(e2e): S14 admin edit through branch protection" \
    || { echo "${ARTEA_ADMIN_USER} contents-API edit failed under branch protection"; return 1; }
  put_policy_file policy.toml "${ORIG_POLICY}" "test(e2e): S14 revert admin probe" || return 1
  POLICY_DIRTY=0
  echo "${ARTEA_ADMIN_USER} contents-API edits on main still work (push allowlist)"

  delete_dev1_token "${S14_TOKEN_NAME}"
}

# ---- S15: fail-closed on lost policy state (npm file + devpi volume) ---------------------------
npm_outage_rejected() { # tarballs 503 AND packument stripped to zero versions
  [ "$(http_code -u "dev1:${DEV1_TOKEN}" "${TARBALL_BLOCKED}")" = 503 ] || return 1
  curl -s -u "dev1:${DEV1_TOKEN}" "${GATEWAY_URL}/npm/left-pad" \
    | jq -e '(.versions // {}) | length == 0' >/dev/null
}
npm_recovered() {
  [ "$(http_code -u "dev1:${DEV1_TOKEN}" "${TARBALL_BLOCKED}")" = 302 ] || return 1
  curl -s -u "dev1:${DEV1_TOKEN}" "${GATEWAY_URL}/npm/left-pad" \
    | jq -e '.versions | length > 0' >/dev/null
}
six_blocked() { # fresh '*'-seeded mirror exposes no six files through the gateway
  local body code
  body=$(curl -s -w '\n%{http_code}' -u "dev1:${DEV1_TOKEN}" "${GATEWAY_URL}/pypi/simple/six/") || return 1
  code=${body##*$'\n'}
  body=${body%$'\n'*}
  [ "$code" = 404 ] && return 0
  [ "$code" = 200 ] || return 1
  ! echo "$body" | grep -q 'six-1\.'
}
six_served() {
  local body
  body=$(gw_get /pypi/simple/six/) || return 1
  grep -q 'six-1\.' <<<"$body"
}
# S13/S14 policy pushes leave webhook syncs (with 2/4/8s retries) in flight; one
# landing right after the wipe would heal the fresh devpi before the '*' seed
# can be observed. Quiesce = a successful last sync and none started since.
PS_LAST_SYNC_PY='import json,urllib.request; d=json.load(urllib.request.urlopen("http://127.0.0.1:8920/healthz", timeout=3)); print(d.get("last_sync_ok"), d.get("last_sync_at"))'
policy_sync_state() { # "<last_sync_ok> <last_sync_at>" via in-container python
  kc exec "deploy/${K8S_POLICY_SYNC_DEPLOY}" -- python -c "${PS_LAST_SYNC_PY}" 2>/dev/null
}
policy_sync_quiesced() {
  local before after
  before=$(policy_sync_state) || return 1
  sleep 5 # longer than the first two retry backoffs (2s/4s)
  after=$(policy_sync_state) || return 1
  [ "$after" = "$before" ] && [ "${after%% *}" = "True" ]
}

s15_fail_closed() {
  local report="${WORK}/s15-report.json" url
  # --- npm: policy source lost (simulated policy-sync outage) ----------------
  # HTTP-delivery mode (policy_url): an outage = policy-sync unreachable. The
  # plugin serves last-known-good until its grace window expires, then fails
  # closed — the rejected state shows up only after ~POLICY_GRACE_SECS.
  kc scale "deployment/${K8S_POLICY_SYNC_DEPLOY}" --replicas=0 \
    || { echo "scaling policy-sync to 0 failed"; return 1; }
  POLICY_SYNC_SCALED=1
  wait_for "$((POLICY_GRACE_SECS + 45))" 3 "public npm rejected once the fail-closed grace window expires" \
    npm_outage_rejected || return 1
  echo "outage: tarball -> 503, packument -> zero versions (nothing served unfiltered)"
  kc scale "deployment/${K8S_POLICY_SYNC_DEPLOY}" --replicas=1 \
    || { echo "scaling policy-sync back up failed"; return 1; }
  kc rollout status "deployment/${K8S_POLICY_SYNC_DEPLOY}" --timeout=120s >/dev/null \
    || { echo "policy-sync did not come back"; return 1; }
  wait_for 45 2 "npm recovered after policy-sync is back" npm_recovered || return 1
  POLICY_SYNC_SCALED=0
  echo "recovery: policy_url poll picked the policy back up without a verdaccio restart"

  # --- pypi: wiped devpi cache comes back fail-closed until policy-sync syncs
  wait_for 60 1 "policy-sync idle (no in-flight sync to race the wipe)" policy_sync_quiesced || return 1
  DEVPI_WIPED=1
  # capture a minimal PVC manifest so the wipe can recreate it 1:1
  kc get pvc "${K8S_DEVPI_PVC}" -o json | jq '{apiVersion, kind,
      metadata: {name: .metadata.name, namespace: .metadata.namespace,
                 labels: (.metadata.labels // {})},
      spec: {accessModes: .spec.accessModes,
             resources: {requests: .spec.resources.requests},
             storageClassName: .spec.storageClassName}}' > "${WORK}/devpi-pvc.json" \
    || { echo "cannot snapshot PVC ${K8S_DEVPI_PVC}"; return 1; }
  kc scale "deployment/${K8S_DEVPI_DEPLOY}" --replicas=0 \
    || { echo "scaling devpi to 0 failed"; return 1; }
  # delete blocks (pvc-protection finalizer) until the devpi pod is gone
  kc delete pvc "${K8S_DEVPI_PVC}" --timeout=90s >/dev/null \
    || { echo "removing devpi PVC failed"; return 1; }
  kc apply -f "${WORK}/devpi-pvc.json" >/dev/null \
    || { echo "recreating devpi PVC failed"; return 1; }
  kc scale "deployment/${K8S_DEVPI_DEPLOY}" --replicas=1 \
    || { echo "scaling devpi back up failed"; return 1; }
  kc rollout status "deployment/${K8S_DEVPI_DEPLOY}" --timeout=180s >/dev/null \
    || { echo "devpi did not come back on the fresh PVC"; return 1; }
  wait_for 15 1 "fresh mirror serves nothing (entrypoint's '*' seed)" six_blocked || return 1
  # heal via the real trigger: a policy-repo push webhook fires a full sync,
  # and policy-sync compares the LIVE index config, so the (re-compiled)
  # constraints still replace the '*' seed. A byte-change to policy.toml is
  # enough to fire the webhook.
  POLICY_DIRTY=1
  put_policy_file policy.toml "${ORIG_POLICY}"$'\n'"# e2e S15 sync trigger" \
    "test(e2e): S15 trigger policy sync" || return 1
  wait_for 60 2 "six served again after policy-sync heals devpi" six_served || return 1
  put_policy_file policy.toml "${ORIG_POLICY}" "test(e2e): S15 revert sync trigger" || return 1
  POLICY_DIRTY=0
  DEVPI_WIPED=0
  # S8 works again end-to-end: pip install six lands on the devpi mirror path
  pip_e2e install -q --index-url "${INDEX_URL}" --force-reinstall --no-deps \
    --report "$report" six || { echo "pip install six failed after recovery"; return 1; }
  url=$(jq -r '.install[0].download_info.url' "$report")
  echo "post-recovery six downloaded from: ${url}"
  assert_origin devpi "$url" "six did not come through the devpi pull-through path" || return 1
}

# ---- S16: PEP 503 normalization cannot dodge the private shadow --------------------------------
s16_normalization() {
  local s body code report="${WORK}/s16-report.json" url
  # curl probes: non-canonical spellings, with and without the trailing slash,
  # must get Gitea's page (its file URL shape), never the devpi mirror's
  for s in "${PY_NAME_CASE}/" "${PY_NAME_UNDERSCORE}"; do
    body=$(curl -s -w '\n%{http_code}' -u "dev1:${DEV1_TOKEN}" "${GATEWAY_URL}/pypi/simple/${s}")
    code=${body##*$'\n'}
    body=${body%$'\n'*}
    [ "$code" = 200 ] || { echo "GET /pypi/simple/${s} -> HTTP ${code}, expected 200"; return 1; }
    assert_contains "/api/packages/${ARTEA_NAMESPACE}/pypi/files/" "$body" \
      "/pypi/simple/${s}: no Gitea file URLs — not the private package" || return 1
    assert_not_contains "/root/" "$body" \
      "/pypi/simple/${s}: devpi mirror URLs leaked into the response" || return 1
    echo "/pypi/simple/${s} -> 200 with Gitea file URLs only"
  done
  # pip, fed a non-canonical spelling, must install the private wheel from Gitea
  pip_e2e install -q --index-url "${INDEX_URL}" --force-reinstall --no-deps \
    --report "$report" "${PY_NAME_UNDERSCORE}==${PY_VERSION}" || { echo "pip install ${PY_NAME_UNDERSCORE} failed"; return 1; }
  url=$(jq -r '.install[0].download_info.url' "$report")
  echo "pip downloaded from: ${url}"
  assert_origin gitea-pypi "$url" "wheel did not come from Gitea" || return 1
}

# ---- S17: legacy scoped .npmrc still works; encoded private-scope paths route to Gitea ------
s17_legacy_and_encoding() {
  local proj="${WORK}/proj-s17" npmrc_legacy="${WORK}/npmrc-legacy"
  local spelling body code out version

  # a. the legacy two-registry client config must keep working unchanged
  write_npmrc_legacy "${npmrc_legacy}" "${DEV1_TOKEN}"
  mkdir -p "$proj"
  echo '{"name":"e2e-consumer-s17","version":"1.0.0"}' > "$proj/package.json"
  (cd "$proj" && npm_config_userconfig="${npmrc_legacy}" npm_config_cache="${WORK}/npm-cache-s17" \
    npm install "${NPM_NAME}@${NPM_VERSION}") || { echo "legacy-npmrc npm install failed"; return 1; }
  version=$(jq -r .version "$proj/node_modules/${NPM_NAME}/package.json")
  assert_eq "${NPM_VERSION}" "$version" "legacy install version mismatch" || return 1
  echo "legacy two-registry .npmrc installs ${NPM_NAME}@${NPM_VERSION}"

  # b. packuments under /npm/: encoded and literal private-scope spellings both reach Gitea
  for spelling in "${NPM_NAME_ENC}" "${NPM_NAME}"; do
    body=$(curl -s -w '\n%{http_code}' -u "dev1:${DEV1_TOKEN}" "${GATEWAY_URL}/npm/${spelling}")
    code=${body##*$'\n'}
    body=${body%$'\n'*}
    [ "$code" = 200 ] || { echo "GET /npm/${spelling} -> HTTP ${code}, expected 200"; return 1; }
    assert_contains "\"${NPM_VERSION}\"" "$body" \
      "/npm/${spelling}: packument does not contain ${NPM_VERSION}" || return 1
    echo "/npm/${spelling} -> 200, packument contains ${NPM_VERSION}"
  done

  # c. dist-tag API route (the /npm/-/package/<scope>/... map entry)
  out=$(npm_e2e dist-tag ls "${NPM_NAME}" 2>&1) || { echo "npm dist-tag ls failed:"; echo "$out"; return 1; }
  echo "$out"
  grep -q '^latest:' <<<"$out" || { echo "no latest tag in dist-tag output"; return 1; }
  echo "dist-tag route serves a latest tag for ${NPM_NAME}"

  # d. boundary: <scope>-evil must NOT be captured by the configured scope route — it falls
  # through to Verdaccio, which misses on npmjs (404). 400 would mean the
  # gateway's encoded-path rejection fired; 401 would mean Gitea answered.
  assert_code 404 -u "dev1:${DEV1_TOKEN}" "${GATEWAY_URL}/npm/@${ARTEA_NAMESPACE}-evil%2fnope" \
    || { echo "expected 404 (Verdaccio miss), not the Gitea route"; return 1; }
  echo "/npm/@${ARTEA_NAMESPACE}-evil%2fnope -> 404 from the Verdaccio path, not the Gitea route"

  # e. the private-scope route is guarded before Gitea; unauthenticated callers
  # get the gateway Basic challenge.
  assert_code 401 "${GATEWAY_URL}/npm/${NPM_NAME_ENC}" || return 1
  echo "anonymous /npm/${NPM_NAME_ENC} -> 401"
}

# ---- S20: PEP 700 upload-time enrichment of the JSON Simple API --------------------------------
# Proves the end-to-end mechanism the routing unit test only stubs: a real
# Accept: application/vnd.pypi.simple.v1+json request through the gateway is
# enriched to api-version 1.1 with per-file upload-time, for BOTH the public
# (devpi pull-through) and private (Gitea) branches, and that a real
# time-filtering client (pip --uploaded-prior-to) can install through it.
pypi_json() { # <token> <name> -> v1+json Simple API body (enriched) or non-zero
  curl -sf -H "Accept: ${PYPI_JSON_ACCEPT}" -u "dev1:$1" \
    "${GATEWAY_URL}/pypi/simple/$2/"
}

s20_pep700_upload_time() {
  local body report="${WORK}/s20-report.json" url stamped
  # a. PUBLIC branch (six -> devpi pull-through). The Gitea-first probe 404s, so
  #    enrichment joins devpi's constrained list with PyPI JSON upload-time.
  body=$(pypi_json "${DEV1_TOKEN}" six) || { echo "public v1+json fetch failed"; return 1; }
  jq -e '.meta["api-version"] == "1.1"' >/dev/null <<<"$body" \
    || { echo "public index is not api-version 1.1"; echo "$body" | head -c 400; return 1; }
  jq -e '[.files[] | select(.["upload-time"])] | length > 0' >/dev/null <<<"$body" \
    || { echo "public index has no file with upload-time"; return 1; }
  # every stamped file's upload-time must be canonical ISO-8601 UTC (Z suffix)
  jq -e '[.files[] | select(.["upload-time"]) | select(.["upload-time"] | test("Z$") | not)] | length == 0' \
    >/dev/null <<<"$body" || { echo "a public upload-time is not Z-suffixed UTC"; return 1; }
  echo "public six: api-version 1.1 with $(jq '[.files[]|select(.["upload-time"])]|length' <<<"$body") upload-time-stamped files"

  # b. PRIVATE branch (the S6/S7 fixture in Gitea). Gitea serves only PEP 503
  #    HTML; the gateway must synthesize v1.1 JSON with per-version created_at.
  body=$(pypi_json "${DEV1_TOKEN}" "${PY_NAME}") || { echo "private v1+json fetch failed"; return 1; }
  jq -e '.meta["api-version"] == "1.1"' >/dev/null <<<"$body" \
    || { echo "private index is not api-version 1.1"; echo "$body" | head -c 400; return 1; }
  # the private file URLs must still be Gitea's (precedence preserved on JSON)
  jq -e --arg ns "${ARTEA_NAMESPACE}" \
    '[.files[] | select(.url | contains("/api/packages/" + $ns + "/pypi/files/"))] | length > 0' \
    >/dev/null <<<"$body" || { echo "private index files are not Gitea-hosted"; return 1; }
  stamped=$(jq '[.files[] | select(.["upload-time"])] | length' <<<"$body")
  [ "${stamped}" -ge 1 ] \
    || { echo "private index has no file with upload-time (Gitea created_at not surfaced)"; return 1; }
  echo "private ${PY_NAME}: api-version 1.1, Gitea-hosted files, ${stamped} upload-time-stamped"

  # c. non-JSON path is unchanged: a plain Accept must still get PEP 503 HTML,
  #    not the enriched JSON (regression guard for the byte-for-byte path).
  body=$(gw_get /pypi/simple/six/) \
    || { echo "non-JSON six fetch failed"; return 1; }
  grep -q 'api-version' <<<"$body" && { echo "non-JSON path leaked JSON enrichment"; return 1; }
  echo "non-JSON path still serves PEP 503 HTML (no enrichment)"

  # d. functional: a real time-filtering client installs through the enriched
  #    index. pip --uploaded-prior-to needs >= 24.1; skip gracefully if older
  #    (the curl assertions above already prove upload-time is present).
  if pip_e2e download --help 2>&1 | grep -q -- '--uploaded-prior-to'; then
    rm -rf "${WORK}/s20-dl" && mkdir -p "${WORK}/s20-dl"
    # a date far in the future accepts all existing releases; the point is that
    # the index now PROVIDES upload-time so the flag no longer hard-errors.
    pip_e2e download -q --index-url "${INDEX_URL}" --no-deps -d "${WORK}/s20-dl" \
      --uploaded-prior-to 2999-01-01T00:00:00Z six \
      || { echo "pip --uploaded-prior-to install of six failed against the enriched index"; return 1; }
    echo "pip --uploaded-prior-to resolved six: $(ls "${WORK}/s20-dl" | head -1)"
    # private package too: proves the Gitea-branch upload-time is consumable
    rm -rf "${WORK}/s20-dl-priv" && mkdir -p "${WORK}/s20-dl-priv"
    pip_e2e download -q --index-url "${INDEX_URL}" --no-deps -d "${WORK}/s20-dl-priv" \
      --uploaded-prior-to 2999-01-01T00:00:00Z "${PY_NAME}==${PY_VERSION}" \
      || { echo "pip --uploaded-prior-to install of the private package failed"; return 1; }
    echo "pip --uploaded-prior-to resolved ${PY_NAME}: $(ls "${WORK}/s20-dl-priv" | head -1)"
  else
    echo "pip lacks --uploaded-prior-to (< 24.1); skipped the install leg (curl assertions cover metadata)"
  fi
}

# ---- S23: PEP 658/714 Core Metadata for public (devpi pull-through) packages ------------------
# Metadata-only resolves (pip/uv fetching a wheel's METADATA instead of the whole
# wheel) require the public Simple API to advertise `core-metadata` (HTML
# data-core-metadata + JSON key) AND the `<wheel>.metadata` file to be downloadable
# through the gateway file guard. Private (Gitea) packages have NO PEP 658 — Gitea
# upstream serves no .metadata file — so the gateway must NOT advertise it for them
# (advertising a file it cannot serve would break metadata-aware installers).
s23_pep658_core_metadata() {
  local html json url meta

  # a. public HTML Simple API advertises the PEP 714 attribute
  html=$(gw_get /pypi/simple/six/) || { echo "six simple HTML fetch failed"; return 1; }
  grep -q 'data-core-metadata' <<<"$html" \
    || { echo "public simple HTML lacks data-core-metadata"; echo "$html" | head -c 300; return 1; }
  echo "public six: HTML simple page advertises data-core-metadata"

  # b. public JSON Simple API carries core-metadata per wheel (survives PEP 700 enrichment)
  json=$(pypi_json "${DEV1_TOKEN}" six) || { echo "six v1+json fetch failed"; return 1; }
  jq -e '[.files[] | select(.filename | endswith(".whl")) | select(.["core-metadata"])] | length > 0' \
    >/dev/null <<<"$json" || { echo "public JSON has no wheel with core-metadata"; return 1; }
  echo "public six: v1+json carries core-metadata per wheel"

  # c. the METADATA file itself downloads at <wheel-url>.metadata through the
  #    gateway (devpi pull-through + the Artea file guard gating it like the wheel)
  meta=""
  while read -r url; do
    [ -n "$url" ] || continue
    # --compressed: devpi serves .metadata with Content-Encoding: gzip, which the
    # gateway streams through untouched (real installers decompress; curl needs the flag).
    if meta=$(curl -sf --compressed -u "dev1:${DEV1_TOKEN}" "${url}.metadata" 2>/dev/null) \
       && grep -qi '^Metadata-Version:' <<<"$meta"; then
      echo "public six: ${url##*/}.metadata served through the gateway ($(printf %s "$meta" | wc -c) bytes)"
      break
    fi
    meta=""
  done < <(jq -r '.files[] | select(.filename | endswith(".whl")) | .url' <<<"$json")
  [ -n "$meta" ] || { echo "no six wheel served a .metadata document through the gateway"; return 1; }

  # d. private packages intentionally advertise NO core-metadata (Gitea upstream gap)
  json=$(pypi_json "${DEV1_TOKEN}" "${PY_NAME}") || { echo "private v1+json fetch failed"; return 1; }
  jq -e '[.files[] | select(.["core-metadata"])] | length == 0' >/dev/null <<<"$json" \
    || { echo "private package unexpectedly advertises core-metadata (Gitea cannot serve .metadata)"; return 1; }
  echo "private ${PY_NAME}: no core-metadata advertised (correct — Gitea has no PEP 658)"
}

# ---- S18-S19: unified policy.toml (ADR-0007) --------------------------------------------------
# These scenarios author non-trivial policy.toml documents and revert to
# ORIG_POLICY afterward (POLICY_DIRTY guards the cleanup revert). They exercise
# the unified compile semantics in docs/policy-schema.md.

# S18: allow-wins — a specific exact-version allow beats a broader whole-package
# deny. deny left-pad (whole) + allow left-pad ==1.3.0 compiles to the semver
# complement `<1.3.0 || >1.3.0`, so 1.3.0 stays VISIBLE while every other version
# is blocked. (docs/policy-schema.md "Precedence and evaluation (allow-wins)".)
left_pad_only_130_visible() { # 1.3.0 present AND at least one other version hidden
  local body
  body=$(gw_get /npm/left-pad) || return 1
  grep -q '"1.3.0":' <<<"$body" || return 1   # the carved-out version stays
  ! grep -q '"1.2.0":' <<<"$body"             # a broader-deny version is gone
}

s18_unified_allow_wins() {
  local body
  POLICY_DIRTY=1
  put_policy_file policy.toml "$(cat <<'TOML'
# e2e fixture (S18) — unified policy.toml: allow-wins exact-version carve-out;
# reverted by the suite. Whole-package deny of left-pad, but 1.3.0 is explicitly
# allowed, so the compiler emits the complement `<1.3.0 || >1.3.0` and 1.3.0 stays.
schema = 1

[defaults]
action = "allow"

[[rules]]
ecosystem = "npm"
name = "left-pad"
action = "deny"
reason = "e2e fixture (S18): broad deny"

[[rules]]
ecosystem = "npm"
name = "left-pad"
versions = "1.3.0"
action = "allow"
reason = "e2e fixture (S18): vetted exact version"
TOML
)" "test(e2e): S18 allow 1.3.0 beats whole-package deny" || return 1
  wait_for 45 2 "only left-pad 1.3.0 visible (allow-wins carve-out)" left_pad_only_130_visible || return 1
  body=$(gw_get /npm/left-pad) || { echo "packument fetch failed"; return 1; }
  grep -q '"1.3.0":' <<<"$body" || { echo "1.3.0 (allowed) is missing"; return 1; }
  grep -q '"1.2.0":' <<<"$body" && { echo "1.2.0 (denied) is still present"; return 1; }
  echo "allow-wins: left-pad 1.3.0 visible, 1.2.0 blocked by the broader deny"
  put_policy_file policy.toml "${ORIG_POLICY}" "test(e2e): S18 revert policy" || return 1
  wait_for 45 2 "left-pad 1.2.0 visible again after revert" left_pad_130_visible || return 1
  POLICY_DIRTY=0
}

# S19: a malformed policy.toml keeps last-known-good. policy-sync validates the
# whole policy before applying; a structural error fails the sync, the previously
# applied policy stays in effect, /healthz reports last_sync_ok:false, and public
# fetch keeps working. (docs/policy-schema.md "Validation".)
s19_unified_last_known_good() {
  local state
  POLICY_DIRTY=1
  # 1. a VALID policy.toml that blocks left-pad 1.3.0; wait until applied
  put_policy_file policy.toml "$(cat <<'TOML'
# e2e fixture (S19) — valid baseline blocking left-pad 1.3.0; a malformed edit
# below must NOT tear this down (last-known-good). Reverted by the suite.
schema = 1

[defaults]
action = "allow"

[[rules]]
ecosystem = "npm"
name = "left-pad"
versions = "1.3.0"
action = "deny"
reason = "e2e fixture (S19): last-known-good baseline"
TOML
)" "test(e2e): S19 valid baseline (block left-pad 1.3.0)" || return 1
  wait_for 45 2 "left-pad 1.3.0 blocked by the valid baseline" left_pad_130_hidden || return 1
  echo "baseline applied: left-pad 1.3.0 hidden"

  # 2. push a structurally BROKEN policy.toml (a rule with neither name nor
  # namespace) — policy_model rejects it, the sync fails, nothing is re-applied
  put_policy_file policy.toml "$(cat <<'TOML'
# e2e fixture (S19) — deliberately malformed: a rule with neither name nor
# namespace is a structural error; policy-sync must keep last-known-good.
schema = 1

[defaults]
action = "allow"

[[rules]]
ecosystem = "npm"
action = "deny"
TOML
)" "test(e2e): S19 malformed policy.toml (must keep last-known-good)" || return 1

  # 3a. the previously-applied block STAYS in effect after the failed sync
  sleep 10 # allow at least one poll/webhook sync attempt to fail
  left_pad_130_hidden || { echo "block was lost after the malformed push — NOT last-known-good"; return 1; }
  echo "last-known-good held: left-pad 1.3.0 still hidden after the malformed push"

  # 3b. public fetch still works (the packument still resolves with versions;
  # 1.3.0 stays filtered per 3a, but the rest of the package is served)
  gw_get /npm/left-pad \
    | jq -e '.versions | length > 0' >/dev/null \
    || { echo "public fetch broke during the malformed-policy window"; return 1; }
  echo "public fetch still works (left-pad packument still has versions)"

  # 3c. /healthz reports the failed sync (best-effort; helper from S15)
  state=$(policy_sync_state || true)
  echo "policy-sync /healthz state (last_sync_ok last_sync_at): ${state:-<unavailable>}"
  case "$state" in
    False*) echo "/healthz confirms last_sync_ok:false" ;;
    "") echo "note: /healthz unavailable in this runtime; skipping the last_sync_ok assertion" ;;
    *) echo "warning: expected last_sync_ok:false after a malformed push, saw: ${state}" ;;
  esac

  # 4. revert to the default-allow baseline -> left-pad fully visible again
  put_policy_file policy.toml "${ORIG_POLICY}" "test(e2e): S19 revert policy" || return 1
  wait_for 45 2 "left-pad 1.3.0 visible again after revert" left_pad_130_visible || return 1
  POLICY_DIRTY=0
}

# ---- S21: pnpm publish private scoped package -> Gitea, then pnpm add resolves it --------
s21_pnpm_publish() {
  local pkgdir="${WORK}/pnpm-hello" proj="${WORK}/proj-s21" version
  make_npm_pkg "${pkgdir}" "${PNPM_NAME}" "${PNPM_VERSION}"
  write_npmrc "${pkgdir}/.npmrc" "${DEV1_TOKEN}"
  (cd "${pkgdir}" && pnpm_e2e publish --no-git-checks) || { echo "pnpm publish failed"; return 1; }
  pkg_version_exists npm "${PNPM_NAME_ENC}" "${PNPM_VERSION}" \
    || { echo "Gitea does not list ${PNPM_NAME}@${PNPM_VERSION} after pnpm publish"; return 1; }
  echo "Gitea package API confirms ${PNPM_NAME}@${PNPM_VERSION}"
  # install it back through the gateway with pnpm (full publish -> consume loop)
  mkdir -p "${proj}"
  echo '{"name":"e2e-consumer-s21","version":"1.0.0"}' > "${proj}/package.json"
  write_npmrc "${proj}/.npmrc" "${DEV1_TOKEN}"
  (cd "${proj}" && pnpm_e2e add "${PNPM_NAME}@${PNPM_VERSION}") || { echo "pnpm add failed"; return 1; }
  version=$(jq -r .version "${proj}/node_modules/${PNPM_NAME}/package.json") \
    || { echo "${PNPM_NAME} not present in node_modules after pnpm add"; return 1; }
  assert_eq "${PNPM_VERSION}" "$version" "pnpm-installed version mismatch" || return 1
  echo "pnpm add resolved ${PNPM_NAME}@${version} from Artea"
}

# ---- S22: uv build + uv publish private wheel -> Gitea, then uv pip install resolves it ---
s22_uv_publish() {
  local pkgdir="${WORK}/uv-hello" target="${WORK}/s22-lib"
  make_py_pkg "${pkgdir}" "${UV_NAME}" "${UV_VERSION}"
  "${VENV}/bin/uv" build --wheel --out-dir "${pkgdir}/dist" "${pkgdir}" \
    || { echo "uv build failed"; return 1; }
  "${VENV}/bin/uv" publish \
    --publish-url "${GATEWAY_URL}/api/packages/${ARTEA_NAMESPACE}/pypi/" \
    --username dev1 --password "${DEV1_TOKEN}" "${pkgdir}/dist/"* \
    || { echo "uv publish failed"; return 1; }
  pkg_version_exists pypi "${UV_NAME}" "${UV_VERSION}" \
    || { echo "Gitea does not list ${UV_NAME} ${UV_VERSION} after uv publish"; return 1; }
  echo "Gitea package API confirms ${UV_NAME} ${UV_VERSION}"
  # install it back through the gateway simple index with uv (full loop)
  "${VENV}/bin/uv" pip install --target "${target}" --index-url "${INDEX_URL}" \
    --no-deps --reinstall "${UV_NAME}==${UV_VERSION}" \
    || { echo "uv pip install failed"; return 1; }
  [ -f "${target}/${UV_MODULE}.py" ] \
    || { echo "${UV_MODULE}.py not present after uv pip install --target ${target}"; return 1; }
  echo "uv pip install resolved ${UV_NAME}==${UV_VERSION} from Artea"
}

# ---- run ---------------------------------------------------------------------------------------
validate_scenario_selection

# Count selected scenarios up front: the parallel phase runs scenarios in
# background subshells that cannot update a parent counter.
SELECTED_SCENARIOS=0
for _id in ${ALL_SCENARIOS}; do
  scenario_selected "$_id" && SELECTED_SCENARIOS=$((SELECTED_SCENARIOS + 1))
done

# S1 first: every other scenario assumes a healthy, bootstrapped stack.
run_scenario S1 "bootstrap state: stack healthy, org/repo/webhook/PATs present" s1_bootstrap

# Parallel phase — independent publish round-trips. Each chain publishes its OWN
# unique package(s) and shares nothing with the others (distinct names, no
# policy.toml mutation, separate tool caches), so they run concurrently; within a
# chain, install-back follows publish. Everything else stays serial below: the
# policy/public/stack-teardown scenarios share state, and S11/S16/S17/S20 read the
# private packages these chains publish. E2E_PARALLEL=0 forces fully sequential.
chain_npm() {
  run_scenario S2 "npm publish ${NPM_NAME}@${NPM_VERSION} -> Gitea" s2_npm_publish
  run_scenario S3 "npm install ${NPM_NAME} resolves from Gitea (gateway scope routing)" s3_npm_install_private
}
chain_pypi() {
  run_scenario S6 "twine upload ${PY_NAME} ${PY_VERSION} -> Gitea" s6_twine_upload
  run_scenario S7 "pip install ${PY_NAME} via the gateway index" s7_pip_install_private
}
chain_shadow() { run_scenario S9 "private ${SHADOW_NAME} fully shadows the PyPI name" s9_precedence_shadowing; }
chain_pnpm()   { run_scenario S21 "pnpm publish ${PNPM_NAME}@${PNPM_VERSION} -> Gitea, then pnpm add resolves it" s21_pnpm_publish; }
chain_uv()     { run_scenario S22 "uv publish ${UV_NAME} ${UV_VERSION} -> Gitea, then uv pip install resolves it" s22_uv_publish; }

if [ "${E2E_PARALLEL:-1}" != 0 ]; then
  # `trap - EXIT` in each subshell so a finishing chain does not fire the parent's
  # cleanup; the main shell still cleans up on its own exit.
  ( trap - EXIT; chain_npm )    &
  ( trap - EXIT; chain_pypi )   &
  ( trap - EXIT; chain_shadow ) &
  ( trap - EXIT; chain_pnpm )   &
  ( trap - EXIT; chain_uv )     &
  wait
else
  chain_npm; chain_pypi; chain_shadow; chain_pnpm; chain_uv
fi

# Serial phase — shared policy.toml, public-package visibility, stack teardown
# (S15), and governance (S14) must not run concurrently.
run_scenario S4 "npm install left-pad via Verdaccio pull-through" s4_npm_install_public
run_scenario S5 "policy.toml push hides left-pad 1.3.0 from npm view" s5_npm_policy_block
run_scenario S8 "pip install six via gateway -> devpi -> PyPI" s8_pip_install_public
run_scenario S10 "policy.toml constrains urllib3 to <2 via gateway" s10_pypi_policy_constraint
run_scenario S11 "read:package PAT installs but gets 401 on publish" s11_token_scopes
run_scenario S12 "revoked PAT stops installing within 60s" s12_revocation
run_scenario S13 "blocked tarball -> 403; policy-cleared public tarball -> npmjs redirect" s13_tarball_enforcement
run_scenario S14 "dev1 cannot push registry-policy@main; admin allowlist works" s14_branch_protection
run_scenario S15 "fail-closed: missing npm policy / wiped devpi, then recovery" s15_fail_closed
run_scenario S16 "non-canonical pypi spellings still resolve to the private package" s16_normalization
run_scenario S17 "legacy scoped .npmrc still works; encoded private-scope paths route to Gitea" s17_legacy_and_encoding
run_scenario S18 "unified allow-wins: exact-version allow beats a whole-package deny" s18_unified_allow_wins
run_scenario S19 "malformed policy.toml keeps last-known-good; public fetch still works" s19_unified_last_known_good
run_scenario S20 "PEP 700 upload-time: v1+json enriched to api-version 1.1 (public+private)" s20_pep700_upload_time
run_scenario S23 "PEP 658 core-metadata: public simple advertises + .metadata downloadable (private opts out)" s23_pep658_core_metadata

report

if [ "$SELECTED_SCENARIOS" -eq 0 ]; then
  die "E2E_SCENARIOS selected no scenarios: ${E2E_SCENARIOS}"
fi

if [ "$FAILED" = 0 ]; then
  if [ -z "${E2E_SCENARIOS}" ]; then
    log "all ${SELECTED_SCENARIOS} scenarios passed"
  else
    log "${SELECTED_SCENARIOS} selected scenario(s) passed: ${E2E_SCENARIOS}"
  fi
else
  log "FAILURES:"
  for _id in ${ALL_SCENARIOS}; do
    [ -f "${RESULT_DIR}/${_id}" ] || continue
    IFS=$'\t' read -r _st _ _d < "${RESULT_DIR}/${_id}"
    [ "$_st" = FAIL ] && printf '%s %s\n' "$_id" "$_d"
  done
fi
exit "$FAILED"
