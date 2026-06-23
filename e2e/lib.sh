# Shared helpers for the Artea e2e suite (sourced by e2e/run.sh).
# Everything here assumes: cwd = repo root, credentials.env already sourced.

log() { echo "[e2e] $*"; }
die() { echo "[e2e] ERROR: $*" >&2; exit 1; }

kc() { # kubectl, namespaced when K8S_NAMESPACE is set
  if [ -n "${K8S_NAMESPACE:-}" ]; then
    kubectl -n "${K8S_NAMESPACE}" "$@"
  else
    kubectl "$@"
  fi
}

http_code() { # <curl args...> -> status code on stdout
  curl -sS -o /dev/null -w '%{http_code}' "$@"
}

# ---- assertion helpers (fold the mechanical shapes run.sh spells by hand) --------
# Each prints a diagnostic and returns 1 on mismatch; scenarios `... || return 1`.
assert_eq() { # <expected> <actual> <message>
  [ "$1" = "$2" ] || { echo "${3}: expected '${1}', got '${2}'"; return 1; }
}

assert_code() { # <expected-code> <curl args...> ; runs http_code on the curl args
  local expected=$1 code
  shift
  code=$(http_code "$@")
  [ "$code" = "$expected" ] || { echo "expected HTTP ${expected}, got ${code} for: $*"; return 1; }
}

assert_contains() { # <needle> <haystack> <message>
  case "$2" in
    *"$1"*) ;;
    *) echo "${3}: '${2}' does not contain '${1}'"; return 1 ;;
  esac
}

assert_not_contains() { # <needle> <haystack> <message>
  case "$2" in
    *"$1"*) echo "${3}: '${2}' unexpectedly contains '${1}'"; return 1 ;;
  esac
}

# Resolve a download/resolved URL against the expected origin glob. Keeps the
# origin globs in one place so scenarios just name the kind.
assert_origin() { # <kind> <url> <message> ; kind: gitea-npm|gitea-pypi|gateway|devpi
  local kind=$1 url=$2 msg=$3
  case "$kind" in
    gitea-npm)  case "$url" in */api/packages/*/npm/*) return 0 ;; esac ;;
    gitea-pypi) case "$url" in */api/packages/*/pypi/files/*) return 0 ;; esac ;;
    gateway)    case "$url" in "${GATEWAY_URL}/npm/"*) return 0 ;; esac ;;
    devpi)      case "$url" in */root/pypi/*) return 0 ;; esac ;;
    *) echo "assert_origin: unknown kind '${kind}'"; return 1 ;;
  esac
  echo "${msg}: '${url}' is not a '${kind}' origin"; return 1
}

gw_get() { # <path> -> body on stdout (dev1 PAT, fail on HTTP error)
  curl -sf -u "dev1:${DEV1_TOKEN}" "${GATEWAY_URL}$1"
}

# ---- Gitea API (admin token) ---------------------------------------------------
# Results land in globals (not stdout) so callers never lose them to subshells.
API_CODE=""
API_BODY=""
admin_api() { # <method> <api path> [json body] -> sets API_CODE + API_BODY
  local tmp
  tmp=$(mktemp)
  API_CODE=$(curl -sS -o "$tmp" -w '%{http_code}' -X "$1" \
    -H "Authorization: token ${ARTEA_ADMIN_TOKEN}" -H 'Content-Type: application/json' \
    ${3:+-d "$3"} "${GATEWAY_URL}/api/v1$2")
  API_BODY=$(cat "$tmp"); rm -f "$tmp"
}

json_b64() { # stdin -> single-line base64
  base64 | tr -d '\n'
}

# ---- policy repo file editing (drives S5/S10 through the real PR-less path) -----
get_policy_file() { # <path in repo> -> raw content on stdout
  curl -sfS -H "Authorization: token ${ARTEA_ADMIN_TOKEN}" \
    "${GATEWAY_URL}/api/v1/repos/${POLICY_REPO}/raw/$1"
}

put_policy_file() { # <path in repo> <new content> <commit message> ; creates or updates
  # Upsert: update an existing file (PUT + sha) or create an absent one (POST).
  # Scenarios author policy.toml whether or not it is already present, so this
  # must handle both create and update.
  local path=$1 content=$2 msg=$3 sha b64 method extra
  admin_api GET "/repos/${POLICY_REPO}/contents/${path}"
  case "$API_CODE" in
    200)
      sha=$(echo "$API_BODY" | jq -r .sha)
      [ -n "$sha" ] && [ "$sha" != null ] \
        || { echo "put_policy_file: no sha for existing ${path}"; return 1; }
      method=PUT; extra=",\"sha\":\"${sha}\"" ;;        # update in place
    404) method=POST; extra="" ;;                        # absent -> create
    *) echo "put_policy_file: GET ${path} -> HTTP ${API_CODE}"; return 1 ;;
  esac
  # content always arrives through $(...) which strips the trailing newline;
  # write it back so policy files keep their POSIX final newline
  b64=$(printf '%s\n' "$content" | json_b64)
  admin_api "${method}" "/repos/${POLICY_REPO}/contents/${path}" \
    "{\"content\":\"${b64}\"${extra},\"message\":$(printf '%s' "$msg" | jq -Rs .)}"
  case "$API_CODE" in 2*) return 0 ;; *) echo "put_policy_file ${path} -> HTTP ${API_CODE}: ${API_BODY}"; return 1 ;; esac
}

# ---- generic polling -------------------------------------------------------------
wait_for() { # <timeout s> <interval s> <description> <command...>
  local timeout=$1 interval=$2 desc=$3 start now
  shift 3
  start=$(date +%s)
  while true; do
    if "$@"; then
      now=$(date +%s)
      echo "wait_for: '${desc}' satisfied after $((now - start))s"
      return 0
    fi
    now=$(date +%s)
    if [ $((now - start)) -ge "$timeout" ]; then
      echo "wait_for: '${desc}' NOT satisfied within ${timeout}s"
      return 1
    fi
    sleep "$interval"
  done
}

# ---- package cleanup helpers ------------------------------------------------------
delete_pkg_version() { # <type> <urlencoded name> <version> ; tolerates 404
  admin_api DELETE "/packages/${ARTEA_NAMESPACE}/$1/$2/$3"
  case "$API_CODE" in 204 | 404) return 0 ;; *) echo "delete ${1}/${2}/${3} -> HTTP ${API_CODE}"; return 1 ;; esac
}

pkg_version_exists() { # <type> <urlencoded name> <version> -> 0 if Gitea has it
  admin_api GET "/packages/${ARTEA_NAMESPACE}/$1/$2/$3"
  [ "$API_CODE" = 200 ]
}

# ---- dev1 token lifecycle (S11/S12) -----------------------------------------------
mint_dev1_token() { # <name> <scopes csv as json array> -> raw token on stdout
  local resp
  resp=$(curl -sfS -u "dev1:${DEV1_PASSWORD}" -X POST -H 'Content-Type: application/json' \
    -d "{\"name\":\"$1\",\"scopes\":$2}" "${GATEWAY_URL}/api/v1/users/dev1/tokens") || return 1
  echo "$resp" | jq -re .sha1
}

delete_dev1_token() { # <name> ; tolerates 404/422 (already gone)
  local code
  code=$(http_code -u "dev1:${DEV1_PASSWORD}" -X DELETE "${GATEWAY_URL}/api/v1/users/dev1/tokens/$1")
  case "$code" in 204 | 404 | 422) return 0 ;; *) echo "delete token $1 -> HTTP ${code}"; return 1 ;; esac
}

# ---- npm helpers -------------------------------------------------------------------
write_npmrc() { # <file> <token> ; the documented single-URL client contract
  # (docs/guides/clients-npm.md): ONE registry — the gateway routes the
  # configured private scope under /npm/ to Gitea server-side (ADR-0002).
  # ONE credential VALUE on two nerf-dart lines (amendment in gateway/nginx.conf):
  #   //host/      — covers Gitea tarball downloads by nerf-dart prefix matching
  #                  (ROOT_URL pins them under /api/packages/<namespace>/npm/...);
  #   //host/npm/  — covers npm publish's local credential preflight, which
  #                  checks only the registry's exact nerf-dart, never //host/.
  local b64
  b64=$(printf 'dev1:%s' "$2" | json_b64)
  cat > "$1" <<EOF
registry=${GATEWAY_URL}/npm/
//${GATEWAY_HOSTPORT}/:_auth=${b64}
//${GATEWAY_HOSTPORT}/npm/:_auth=${b64}
always-auth=true
audit=false
fund=false
update-notifier=false
EOF
}

write_npmrc_legacy() { # <file> <token> ; the OLD two-registry contract — kept as
  # the backward-compat probe (S17): client-side private scope routing to Gitea
  # plus path-scoped credentials. Must keep working unchanged behind the new
  # gateway (the legacy /api/packages/<namespace>/npm/ URLs bypass /npm/ routing).
  local b64
  b64=$(printf 'dev1:%s' "$2" | json_b64)
  cat > "$1" <<EOF
registry=${GATEWAY_URL}/npm/
@${ARTEA_NAMESPACE}:registry=${GATEWAY_URL}/api/packages/${ARTEA_NAMESPACE}/npm/
//${GATEWAY_HOSTPORT}/npm/:_auth=${b64}
//${GATEWAY_HOSTPORT}/api/packages/${ARTEA_NAMESPACE}/npm/:_authToken=$2
always-auth=true
audit=false
fund=false
update-notifier=false
EOF
}

npm_e2e() { # npm with the suite's isolated userconfig + per-run cache
  npm_config_userconfig="${NPMRC}" npm_config_cache="${NPM_CACHE}" npm "$@"
}

make_npm_pkg() { # <dir> <name> <version>
  mkdir -p "$1"
  cat > "$1/package.json" <<EOF
{
  "name": "$2",
  "version": "$3",
  "description": "Artea e2e fixture",
  "main": "index.js",
  "license": "MIT"
}
EOF
  echo "module.exports = 'hello from registry e2e';" > "$1/index.js"
}

# ---- python helpers -----------------------------------------------------------------
make_py_pkg() { # <dir> <dist name> <version> ; trivial single-module wheel source
  local module=${2//-/_}
  mkdir -p "$1"
  cat > "$1/pyproject.toml" <<EOF
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "$2"
version = "$3"
description = "Artea e2e fixture"

[tool.setuptools]
py-modules = ["${module}"]
EOF
  echo "GREETING = 'hello from registry e2e'" > "$1/${module}.py"
}

pip_env() { # run a command with host PIP_* env/config leakage removed
  # (e.g. PIP_UPLOADED_PRIOR_TO breaks indexes without upload-time metadata)
  local unset_args
  unset_args=$(env | sed -n 's/^\(PIP_[A-Za-z_0-9]*\)=.*/-u \1/p' | tr '\n' ' ')
  # shellcheck disable=SC2086
  env $unset_args PIP_CONFIG_FILE=/dev/null "$@"
}

build_wheel() { # <dir> ; wheel lands in <dir>/dist
  pip_env "${VENV}/bin/python" -m build --wheel --no-isolation --outdir "$1/dist" "$1"
}

pip_e2e() { # pip from the suite venv, cache disabled (index freshness matters)
  pip_env "${VENV}/bin/pip" --no-cache-dir --disable-pip-version-check "$@"
}

twine_upload() { # <token> <wheel...> ; uploads to Gitea through the gateway
  local token=$1
  shift
  "${VENV}/bin/twine" upload --non-interactive --disable-progress-bar \
    --repository-url "${GATEWAY_URL}/api/packages/${ARTEA_NAMESPACE}/pypi/" \
    -u dev1 -p "$token" "$@"
}
