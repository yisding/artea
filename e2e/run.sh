#!/usr/bin/env bash
# Artea e2e scenario suite — codifies S1-S12 from docs/ARCHITECTURE.md (the
# definition of done for v1). Requires a running stack (`make up`) and a
# completed bootstrap (`make bootstrap`); uses real client tools: npm with an
# isolated userconfig, pip/twine/build from a venv under e2e/tmp.
#
# Re-runnable: package versions are unique per run, fixed-version fixtures
# (tinynetrc 0.0.1) are deleted up front, and policy edits are reverted.
# Exit code is non-zero when any scenario fails; per-scenario PASS/FAIL with
# logs under e2e/tmp/run-<id>/logs/.
set -uo pipefail
cd "$(dirname "$0")/.."

# shellcheck disable=SC1091
source e2e/lib.sh

for tool in curl jq docker npm python3; do
  command -v "$tool" >/dev/null || die "required tool '${tool}' not found"
done

CRED_FILE=e2e/tmp/credentials.env
[ -f "${CRED_FILE}" ] || die "${CRED_FILE} missing — run 'make bootstrap' first"
# shellcheck disable=SC1090
source "./${CRED_FILE}"
GATEWAY_URL="${GATEWAY_URL:-http://localhost:8080}"
GATEWAY_HOSTPORT="${GATEWAY_URL#http://}"

RUN_ID=$(date +%s)
ROOT=$(pwd)
# absolute paths: npm/pip run from fixture dirs, relative paths would break
WORK="${ROOT}/e2e/tmp/run-${RUN_ID}"
LOG_DIR="${WORK}/logs"
mkdir -p "${LOG_DIR}"

# Per-run package versions keep the suite re-runnable without depending on
# cleanup having succeeded; cleanup deletes them anyway to avoid clutter.
NPM_NAME="@artea/hello-artea"
NPM_NAME_ENC="%40artea%2Fhello-artea"
NPM_VERSION="0.0.${RUN_ID}"
NPM_RO_VERSION="0.1.${RUN_ID}" # plain semver: npm refuses prereleases without --tag
PY_NAME="artea-hello"
PY_VERSION="0.0.${RUN_ID}"
PY_RO_VERSION="0.0.${RUN_ID}.post1"
SHADOW_NAME="tinynetrc" # real PyPI package, published privately as 0.0.1 in S9
SHADOW_VERSION="0.0.1"

RO_TOKEN_NAME="e2e-ro-${RUN_ID}"
REVOKE_TOKEN_NAME="e2e-revoke-${RUN_ID}"

NPMRC="${WORK}/npmrc"
NPM_CACHE="${WORK}/npm-cache"
VENV="${ROOT}/e2e/tmp/venv"

NPM_RULES_DIRTY=0
CONSTRAINTS_DIRTY=0

# ---- cleanup (idempotent, tolerates partial runs) ---------------------------------
cleanup() {
  local rc=$?
  set +e
  if [ "${NPM_RULES_DIRTY}" = 1 ]; then
    put_policy_file npm-rules.yaml "${ORIG_NPM_RULES}" "test(e2e): revert npm rules (cleanup)" >/dev/null
  fi
  if [ "${CONSTRAINTS_DIRTY}" = 1 ]; then
    put_policy_file pypi-constraints.txt "${ORIG_CONSTRAINTS}" "test(e2e): revert pypi constraints (cleanup)" >/dev/null
  fi
  delete_pkg_version npm "${NPM_NAME_ENC}" "${NPM_VERSION}" >/dev/null
  delete_pkg_version npm "${NPM_NAME_ENC}" "${NPM_RO_VERSION}" >/dev/null
  delete_pkg_version pypi "${PY_NAME}" "${PY_VERSION}" >/dev/null
  delete_pkg_version pypi "${PY_NAME}" "${PY_RO_VERSION}" >/dev/null
  delete_pkg_version pypi "${SHADOW_NAME}" "${SHADOW_VERSION}" >/dev/null
  delete_dev1_token "${RO_TOKEN_NAME}" >/dev/null
  delete_dev1_token "${REVOKE_TOKEN_NAME}" >/dev/null
  exit "$rc"
}
trap cleanup EXIT

# ---- suite setup -------------------------------------------------------------------
log "run id ${RUN_ID}; work dir ${WORK}"

ORIG_NPM_RULES=$(get_policy_file npm-rules.yaml) || die "cannot read npm-rules.yaml from the policy repo"
ORIG_CONSTRAINTS=$(get_policy_file pypi-constraints.txt) || die "cannot read pypi-constraints.txt from the policy repo"

write_npmrc "${NPMRC}" "${DEV1_TOKEN}"
mkdir -p "${NPM_CACHE}"

if [ ! -f "${VENV}/.artea-e2e-ready" ]; then
  log "creating python venv with build/twine (one-time, network)"
  python3 -m venv "${VENV}" || die "venv creation failed"
  pip_env "${VENV}/bin/pip" install -q -U pip setuptools wheel build twine || die "venv tool install failed"
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
RESULTS=""
FAILED=0

scenario() { # <id> <description> <function>
  local id=$1 desc=$2 fn=$3 t0 t1 status
  local logf="${LOG_DIR}/${id}.log"
  t0=$(date +%s)
  if "$fn" >"$logf" 2>&1; then
    status=PASS
  else
    status=FAIL
    FAILED=1
  fi
  t1=$(date +%s)
  printf '%-4s %-4s %s (%ss)\n' "$id" "$status" "$desc" "$((t1 - t0))"
  if [ "$status" = FAIL ]; then
    sed 's/^/     | /' "$logf" | tail -25
  fi
  RESULTS="${RESULTS}${id} ${status} ${desc}"$'\n'
}

# ---- S1: bootstrap state ----------------------------------------------------------------
s1_bootstrap() {
  local c login
  for c in gitea verdaccio devpi gateway policy-sync; do
    local health
    health=$(docker inspect -f '{{.State.Health.Status}}' "$c") || return 1
    echo "container ${c}: ${health}"
    [ "$health" = healthy ] || return 1
  done
  admin_api GET /user
  [ "$API_CODE" = 200 ] || { echo "admin token rejected (HTTP ${API_CODE})"; return 1; }
  login=$(echo "$API_BODY" | jq -r .login)
  [ "$login" = "${ARTEA_ADMIN_USER}" ] || { echo "admin token belongs to ${login}"; return 1; }
  admin_api GET /orgs/artea
  [ "$API_CODE" = 200 ] || { echo "org artea missing"; return 1; }
  [ "$(echo "$API_BODY" | jq -r .visibility)" = private ] || { echo "org artea is not private"; return 1; }
  admin_api GET /repos/artea/registry-policy/contents/npm-rules.yaml
  [ "$API_CODE" = 200 ] || { echo "npm-rules.yaml not seeded"; return 1; }
  admin_api GET /repos/artea/registry-policy/contents/pypi-constraints.txt
  [ "$API_CODE" = 200 ] || { echo "pypi-constraints.txt not seeded"; return 1; }
  admin_api GET /repos/artea/registry-policy/hooks
  [ "$API_CODE" = 200 ] || { echo "cannot list hooks"; return 1; }
  echo "$API_BODY" | jq -e 'any(.[]; .config.url == "http://policy-sync:8920/hooks/policy" and .active)' >/dev/null \
    || { echo "policy webhook not wired"; return 1; }
  login=$(curl -sf -H "Authorization: token ${DEV1_TOKEN}" "${GATEWAY_URL}/api/v1/user" | jq -r .login)
  [ "$login" = dev1 ] || { echo "dev1 PAT rejected"; return 1; }
  echo "org, policy repo (both files), webhook, admin+dev1 PATs all present"
}

# ---- S2: npm publish @artea/hello-artea -> 201 in Gitea ----------------------------------
s2_npm_publish() {
  make_npm_pkg "${WORK}/hello-artea" "${NPM_NAME}" "${NPM_VERSION}"
  (cd "${WORK}/hello-artea" && npm_e2e publish --loglevel=http) || { echo "npm publish failed"; return 1; }
  pkg_version_exists npm "${NPM_NAME_ENC}" "${NPM_VERSION}" \
    || { echo "Gitea does not list ${NPM_NAME}@${NPM_VERSION} after publish"; return 1; }
  echo "Gitea package API confirms ${NPM_NAME}@${NPM_VERSION}"
}

# ---- S3: npm install @artea/hello-artea resolves from Gitea ------------------------------
s3_npm_install_private() {
  local proj="${WORK}/proj-s3" resolved version
  mkdir -p "$proj"
  echo '{"name":"e2e-consumer-s3","version":"1.0.0"}' > "$proj/package.json"
  (cd "$proj" && npm_e2e install "${NPM_NAME}@${NPM_VERSION}") || { echo "npm install failed"; return 1; }
  version=$(jq -r .version "$proj/node_modules/${NPM_NAME}/package.json")
  [ "$version" = "${NPM_VERSION}" ] || { echo "installed version ${version}, expected ${NPM_VERSION}"; return 1; }
  resolved=$(jq -r ".packages[\"node_modules/${NPM_NAME}\"].resolved" "$proj/package-lock.json")
  echo "resolved: ${resolved}"
  case "$resolved" in
    */api/packages/artea/npm/*) ;;
    *) echo "tarball did not come from Gitea scope routing"; return 1 ;;
  esac
}

# ---- S4: npm install left-pad via Verdaccio pull-through ----------------------------------
s4_npm_install_public() {
  local proj="${WORK}/proj-s4" resolved
  mkdir -p "$proj"
  echo '{"name":"e2e-consumer-s4","version":"1.0.0"}' > "$proj/package.json"
  (cd "$proj" && npm_e2e install left-pad@1.3.0) || { echo "npm install left-pad failed"; return 1; }
  [ -f "$proj/node_modules/left-pad/package.json" ] || { echo "left-pad not in node_modules"; return 1; }
  resolved=$(jq -r '.packages["node_modules/left-pad"].resolved' "$proj/package-lock.json")
  echo "resolved: ${resolved}"
  case "$resolved" in
    "${GATEWAY_URL}/npm/"*) ;;
    *) echo "tarball did not come through the gateway /npm/ (verdaccio) path"; return 1 ;;
  esac
}

# ---- S5: block left-pad 1.3.0 via npm-rules.yaml push -------------------------------------
left_pad_130_hidden() {
  local body
  body=$(curl -sf -u "dev1:${DEV1_TOKEN}" "${GATEWAY_URL}/npm/left-pad") || return 1
  ! echo "$body" | grep -q '"1.3.0":'
}
left_pad_130_visible() {
  local body
  body=$(curl -sf -u "dev1:${DEV1_TOKEN}" "${GATEWAY_URL}/npm/left-pad") || return 1
  echo "$body" | grep -q '"1.3.0":'
}

s5_npm_policy_block() {
  local rules versions
  # the seed file ends with an empty 'blocked:' mapping; appending would be
  # invalid YAML, so the fixture replaces the whole file (reverted below)
  rules="# e2e S5 fixture — temporarily blocks left-pad 1.3.0; reverted by the suite.
blocked:
  scopes: []
  packages:
    - name: left-pad
      versions: \"1.3.0\"
      reason: e2e S5"
  NPM_RULES_DIRTY=1
  put_policy_file npm-rules.yaml "$rules" "test(e2e): S5 block left-pad 1.3.0" || return 1
  wait_for 45 2 "left-pad 1.3.0 filtered from packument" left_pad_130_hidden || return 1
  versions=$(npm_fresh view left-pad versions --json) || { echo "npm view failed"; return 1; }
  echo "npm view left-pad versions: ${versions}"
  echo "$versions" | jq -e 'length > 0' >/dev/null || { echo "empty versions list"; return 1; }
  echo "$versions" | jq -e 'index("1.3.0") == null' >/dev/null \
    || { echo "1.3.0 still present in npm view output"; return 1; }
  put_policy_file npm-rules.yaml "${ORIG_NPM_RULES}" "test(e2e): S5 revert npm rules" || return 1
  wait_for 45 2 "left-pad 1.3.0 visible again after revert" left_pad_130_visible || return 1
  NPM_RULES_DIRTY=0
}

# ---- S6: twine upload artea-hello wheel -> Gitea -------------------------------------------
s6_twine_upload() {
  make_py_pkg "${WORK}/artea-hello" "${PY_NAME}" "${PY_VERSION}"
  build_wheel "${WORK}/artea-hello" || { echo "wheel build failed"; return 1; }
  twine_upload "${DEV1_TOKEN}" "${WORK}/artea-hello/dist/"*.whl || { echo "twine upload failed"; return 1; }
  pkg_version_exists pypi "${PY_NAME}" "${PY_VERSION}" \
    || { echo "Gitea does not list ${PY_NAME} ${PY_VERSION} after upload"; return 1; }
  echo "Gitea package API confirms ${PY_NAME} ${PY_VERSION} (artifact stored in Gitea)"
}

# ---- S7: pip install artea-hello via the gateway index --------------------------------------
s7_pip_install_private() {
  local report="${WORK}/s7-report.json" url
  pip_e2e install -q --index-url "${INDEX_URL}" --force-reinstall --no-deps \
    --report "$report" "${PY_NAME}==${PY_VERSION}" || { echo "pip install failed"; return 1; }
  url=$(jq -r '.install[0].download_info.url' "$report")
  echo "downloaded from: ${url}"
  case "$url" in
    */api/packages/artea/pypi/files/*) ;;
    *) echo "wheel did not come from Gitea"; return 1 ;;
  esac
}

# ---- S8: pip install six via gateway -> devpi -> PyPI ----------------------------------------
s8_pip_install_public() {
  local report="${WORK}/s8-report.json" url
  pip_e2e install -q --index-url "${INDEX_URL}" --force-reinstall --no-deps \
    --report "$report" six || { echo "pip install six failed"; return 1; }
  url=$(jq -r '.install[0].download_info.url' "$report")
  echo "downloaded from: ${url}"
  case "$url" in
    */root/pypi/*) ;; # devpi mirror file path through the gateway
    *) echo "six did not come through the devpi pull-through path"; return 1 ;;
  esac
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

# ---- S10: pypi-constraints.txt urllib3<2 ------------------------------------------------------
urllib3_v2_hidden() {
  local body
  body=$(curl -sf -u "dev1:${DEV1_TOKEN}" "${GATEWAY_URL}/pypi/simple/urllib3/") || return 1
  ! echo "$body" | grep -q 'urllib3-2\.'
}
urllib3_v2_visible() {
  local body
  body=$(curl -sf -u "dev1:${DEV1_TOKEN}" "${GATEWAY_URL}/pypi/simple/urllib3/") || return 1
  echo "$body" | grep -q 'urllib3-2\.'
}

s10_pypi_policy_constraint() {
  local out line wheel
  CONSTRAINTS_DIRTY=1
  put_policy_file pypi-constraints.txt "${ORIG_CONSTRAINTS}"$'\n'"urllib3<2" \
    "test(e2e): S10 constrain urllib3<2" || return 1
  wait_for 45 2 "urllib3 2.x filtered from simple index" urllib3_v2_hidden || return 1
  out=$(pip_e2e index versions urllib3 --index-url "${INDEX_URL}" 2>&1) \
    || { echo "pip index versions failed: ${out}"; return 1; }
  line=$(echo "$out" | grep '^Available versions:') || { echo "no versions line"; return 1; }
  echo "$line"
  echo "$line" | grep -Eq '(:|, )2\.' && { echo "a 2.x version is still visible"; return 1; }
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
  put_policy_file pypi-constraints.txt "${ORIG_CONSTRAINTS}" "test(e2e): S10 revert constraints" || return 1
  wait_for 45 2 "urllib3 2.x visible again after revert" urllib3_v2_visible || return 1
  CONSTRAINTS_DIRTY=0
}

# ---- S11: one PAT everywhere; read:package can pull but not publish (401) ---------------------
s11_token_scopes() {
  local ro_token out code proj="${WORK}/proj-s11"
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
  code=$(http_code -u "dev1:${ro_token}" "${GATEWAY_URL}/npm/left-pad")
  [ "$code" = 200 ] || { echo "read-only verdaccio pull got HTTP ${code}"; return 1; }
  echo "read-only token installs fine (npm private+public, pip private)"

  # npm publish with the read-only token must be rejected with 401 (not 403)
  make_npm_pkg "${WORK}/hello-artea-ro" "${NPM_NAME}" "${NPM_RO_VERSION}"
  out=$( (cd "${WORK}/hello-artea-ro" && npm_config_userconfig="${WORK}/npmrc-ro" \
    npm_config_cache="${WORK}/npm-cache-ro" npm publish) 2>&1) && {
    echo "npm publish with read-only token unexpectedly succeeded"; return 1; }
  echo "$out" | grep -q 'E401' || { echo "npm publish rejection was not 401:"; echo "$out"; return 1; }
  echo "$out" | grep -q '403' && { echo "got 403, expected 401:"; echo "$out"; return 1; }
  pkg_version_exists npm "${NPM_NAME_ENC}" "${NPM_RO_VERSION}" && { echo "package was created anyway"; return 1; }
  echo "npm publish with read-only token rejected with 401"

  # twine upload with the read-only token must be rejected with 401 (not 403)
  make_py_pkg "${WORK}/artea-hello-ro" "${PY_NAME}" "${PY_RO_VERSION}"
  build_wheel "${WORK}/artea-hello-ro" || { echo "wheel build failed"; return 1; }
  out=$(twine_upload "${ro_token}" "${WORK}/artea-hello-ro/dist/"*.whl 2>&1) && {
    echo "twine upload with read-only token unexpectedly succeeded"; return 1; }
  echo "$out" | grep -q '401' || { echo "twine rejection was not 401:"; echo "$out"; return 1; }
  echo "$out" | grep -q '403' && { echo "got 403, expected 401:"; echo "$out"; return 1; }
  pkg_version_exists pypi "${PY_NAME}" "${PY_RO_VERSION}" && { echo "package was created anyway"; return 1; }
  echo "twine upload with read-only token rejected with 401"

  delete_dev1_token "${RO_TOKEN_NAME}"
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

# ---- run ---------------------------------------------------------------------------------------
scenario S1 "bootstrap state: stack healthy, org/repo/webhook/PATs present" s1_bootstrap
scenario S2 "npm publish ${NPM_NAME}@${NPM_VERSION} -> Gitea" s2_npm_publish
scenario S3 "npm install ${NPM_NAME} resolves from Gitea (scope routing)" s3_npm_install_private
scenario S4 "npm install left-pad via Verdaccio pull-through" s4_npm_install_public
scenario S5 "policy push hides left-pad 1.3.0 from npm view" s5_npm_policy_block
scenario S6 "twine upload ${PY_NAME} ${PY_VERSION} -> Gitea" s6_twine_upload
scenario S7 "pip install ${PY_NAME} via the gateway index" s7_pip_install_private
scenario S8 "pip install six via gateway -> devpi -> PyPI" s8_pip_install_public
scenario S9 "private ${SHADOW_NAME} fully shadows the PyPI name" s9_precedence_shadowing
scenario S10 "constraints push limits urllib3 to <2 via gateway" s10_pypi_policy_constraint
scenario S11 "read:package PAT installs but gets 401 on publish" s11_token_scopes
scenario S12 "revoked PAT stops installing within 60s" s12_revocation

echo
if [ "$FAILED" = 0 ]; then
  log "all 12 scenarios passed"
else
  log "FAILURES:"
  echo "${RESULTS}" | grep ' FAIL ' || true
fi
exit "$FAILED"
