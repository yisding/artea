#!/usr/bin/env bash
# Artea bootstrap (e2e scenario S1). Idempotent: safe to re-run at any time.
#
# Creates/ensures, in order: gitea secret files, admin + PAT, configured private
# namespace org, `${ARTEA_NAMESPACE}/registry-policy` seeded from policy/,
# push webhook -> policy-sync, the `developers` team (code/pulls/packages
# write, no admin), demo user `dev1` (developers member, never Owners) + PAT,
# branch protection on the policy repo's default branch (PRs + >=1 approval;
# direct push only for the configured admin), the `svc-policy` service account
# in a read-only `policy-readers` team, and its policy-sync PAT (delivered via
# the token sink below). Re-running migrates older stacks: dev1 is moved out of
# Owners and an admin-minted POLICY_SYNC_TOKEN is rotated to svc-policy and
# revoked.
#
# Runs as the Helm post-install/upgrade hook Job (Kubernetes only). The minted
# svc-policy PAT is patched into the Secret SECRET_NAME (key SECRET_KEY, default
# POLICY_SYNC_TOKEN) and DEPLOYMENT_NAME is rollout-restarted, both via kubectl in
# NAMESPACE (empty = context default). Admin actions use the Gitea HTTP API (the
# chart provisions the admin user). Set EMIT_CREDENTIALS=true for e2e/dev to print
# credentials to stdout between BEGIN/END markers (the harness extracts them from
# the Job logs) and, if WRITE_CREDENTIALS_PATH is set and writable, write them
# there too.
#
# Other knobs (all optional): GITEA_URL (where this script reaches Gitea),
# GATEWAY_URL (base recorded in the credentials), ARTEA_PUBLIC_URL (public
# browser/client base rendered into the server-side client setup guide),
# GITEA_READY_TIMEOUT, POLICY_SYNC_URL, POLICY_SYNC_HOOK_URL, ARTEA_NAMESPACE,
# ARTEA_ADMIN_USER, ROLLOUT_TIMEOUT. Credentials (ARTEA_ADMIN_PASSWORD,
# DEV1_PASSWORD, POLICY_WEBHOOK_SECRET, POLICY_SYNC_TOKEN) come from the
# environment (the chart's Secrets).
set -euo pipefail
cd "$(dirname "$0")/.."

log() { echo "[bootstrap] $*"; }
die() { echo "[bootstrap] ERROR: $*" >&2; exit 1; }
truthy() { case "${1:-}" in 1 | true | TRUE | yes | YES | on | ON) return 0 ;; *) return 1 ;; esac; }

# GITEA_URL = where THIS script reaches Gitea (in-cluster service URL);
# GATEWAY_URL = the base URL recorded in the credentials for e2e.
GITEA_URL="${GITEA_URL:-${GATEWAY_URL:-http://localhost:8080}}"
GATEWAY_URL="${GATEWAY_URL:-${GITEA_URL}}"
ARTEA_PUBLIC_URL="${ARTEA_PUBLIC_URL:-${GATEWAY_URL}}"
ARTEA_PUBLIC_URL="${ARTEA_PUBLIC_URL%/}"
GITEA_READY_TIMEOUT="${GITEA_READY_TIMEOUT:-120}" # seconds
# policy-sync base URL: polled directly for health; also the default webhook
# target unless POLICY_SYNC_HOOK_URL overrides it
POLICY_SYNC_URL="${POLICY_SYNC_URL:-http://policy-sync:8920}"
POLICY_SYNC_HOOK_URL="${POLICY_SYNC_HOOK_URL:-${POLICY_SYNC_URL}/hooks/policy}"
REPO=registry-policy
CRED_FILE="${WRITE_CREDENTIALS_PATH:-e2e/tmp/credentials.env}"
case "${CRED_FILE}" in /*) ;; *) CRED_FILE="./${CRED_FILE}" ;; esac
RESP="$(mktemp)"
CLIENT_SETUP_FILE="$(mktemp)"
PATCH_FILE=""
trap 'rm -f "$RESP" "$CLIENT_SETUP_FILE" ${PATCH_FILE:+"$PATCH_FILE"}' EXIT

command -v kubectl >/dev/null || die "kubectl is required (bootstrap runs in-cluster as a Helm hook Job)"
# explicit die, not ${VAR:?}: with an EXIT trap set, bash 3.2 exits 0 on a
# failed :? expansion, which would make a misconfigured bootstrap Job "pass"
[ -n "${SECRET_NAME:-}" ] || die "SECRET_NAME is required (the policy-sync token Secret)"
[ -n "${DEPLOYMENT_NAME:-}" ] || die "DEPLOYMENT_NAME is required (the policy-sync Deployment)"
SECRET_KEY="${SECRET_KEY:-POLICY_SYNC_TOKEN}"
ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-120}" # seconds
KC=(kubectl)
[ -n "${NAMESPACE:-}" ] && KC=(kubectl -n "${NAMESPACE}")

wait_deployment_rollout() { # <deployment-name>; k8s mode only
  local name="$1" deadline json
  deadline=$((SECONDS + ROLLOUT_TIMEOUT))
  while :; do
    if json=$("${KC[@]}" get "deployment/${name}" -o json 2>/dev/null); then
      if printf '%s' "${json}" | python3 -c '
import json
import sys

d = json.load(sys.stdin)
meta = d.get("metadata") or {}
spec = d.get("spec") or {}
status = d.get("status") or {}
desired = spec.get("replicas")
if desired is None:
    desired = 1
ready = (
    status.get("observedGeneration", 0) >= meta.get("generation", 0)
    and status.get("updatedReplicas", 0) >= desired
    and status.get("replicas", 0) <= desired
    and status.get("availableReplicas", 0) >= desired
)
sys.exit(0 if ready else 1)
'; then
        return 0
      fi
    fi
    [ "${SECONDS}" -lt "${deadline}" ] || return 1
    sleep 2
  done
}

# Credentials and config come straight from the environment (the chart's Secrets).
ARTEA_NAMESPACE="${ARTEA_NAMESPACE:-artea}"
if ! [[ "${ARTEA_NAMESPACE}" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]]; then
  die "ARTEA_NAMESPACE must be a lowercase npm/Gitea-safe name: [a-z0-9]([a-z0-9-]*[a-z0-9])?"
fi
ADMIN_USER="${ARTEA_ADMIN_USER:-${ARTEA_NAMESPACE}-admin}"
ORG="${ARTEA_NAMESPACE}"
POLICY_REPO="${ORG}/${REPO}"
[ -n "${ARTEA_ADMIN_PASSWORD:-}" ] || die "ARTEA_ADMIN_PASSWORD must be set in the environment"
[ -n "${DEV1_PASSWORD:-}" ] || die "DEV1_PASSWORD must be set in the environment"
[ -n "${POLICY_WEBHOOK_SECRET:-}" ] || die "POLICY_WEBHOOK_SECRET must be set in the environment"
if ! truthy "${ARTEA_ALLOW_DEV_SECRETS:-false}"; then
  # match the .env.example placeholders by PREFIX (mirroring the Helm validator's
  # hasPrefix check) so a real secret that merely contains "change-me-" is allowed
  for _secret in "${ARTEA_ADMIN_PASSWORD}" "${DEV1_PASSWORD}" "${POLICY_WEBHOOK_SECRET}" "${DEVPI_ROOT_PASSWORD:-}"; do
    case "${_secret}" in
      change-me-*) die "change-me placeholder secrets are not allowed; set real secrets or ARTEA_ALLOW_DEV_SECRETS=true for a throwaway dev stack" ;;
    esac
  done
  unset _secret
fi

# ---- helpers -----------------------------------------------------------------
# stdin JSON -> top-level <field> on stdout ('' when absent), the trivial
# field-extractor pattern used throughout this script.
json_get() { # <field>
  python3 -c 'import json,sys; print(json.load(sys.stdin).get(sys.argv[1],""))' "$1"
}

# Poll <cmd...> until it succeeds; 0 on success, 1 once <timeout>s elapse.
# Mirrors the script's existing seq-based waits: <timeout>/<interval> attempts,
# sleeping <interval>s between them. Callers `die` on the non-zero return so the
# die-on-timeout semantics stay at the call site.
retry_until() { # <timeout s> <interval s> <desc> <cmd...>
  local timeout=$1 interval=$2 desc=$3 tries i
  shift 3
  tries=$(( timeout / interval ))
  [ "$tries" -lt 1 ] && tries=1
  for i in $(seq 1 "${tries}"); do
    "$@" && return 0
    [ "$i" -eq "${tries}" ] && break
    sleep "${interval}"
  done
  return 1
}

# all minted tokens get unique names; gitea refuses duplicate token names
mint_token() { # <user> <comma-separated scopes>  -> raw token on stdout
  # admin Basic auth may mint a token for any user
  local scopes
  scopes=$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1].split(",")))' "$2")
  curl -sfS -u "${ADMIN_USER}:${ARTEA_ADMIN_PASSWORD}" -X POST -H 'Content-Type: application/json' \
    -d "{\"name\":\"bootstrap-$(date +%s)-$RANDOM\",\"scopes\":${scopes}}" \
    "${GITEA_URL}/api/v1/users/$1/tokens" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["sha1"])'
}

create_user() { # <username> <password> -> create via the admin API
  # the admin API's email validator rejects dot-less domains like user@localhost,
  # hence the .localhost suffix
  admin_send POST /admin/users \
    "{\"username\":\"$1\",\"email\":\"$1@${ORG}.localhost\",\"password\":\"$2\",\"must_change_password\":false}"
}

admin_code() { # <api path> -> http status code
  curl -sS -o "$RESP" -w '%{http_code}' -H "Authorization: token ${ADMIN_TOKEN}" "${GITEA_URL}/api/v1$1"
}

admin_send() { # <method> <api path> <json body or ''> ; dies on non-2xx
  local code
  code=$(curl -sS -o "$RESP" -w '%{http_code}' -X "$1" \
    -H "Authorization: token ${ADMIN_TOKEN}" -H 'Content-Type: application/json' \
    ${3:+-d "$3"} "${GITEA_URL}/api/v1$2")
  case "$code" in 2*) ;; *) die "$1 $2 -> HTTP $code: $(cat "$RESP")";; esac
}

token_login() { # <token> -> login name of the token's user, '' if invalid
  curl -sf -H "Authorization: token $1" "${GITEA_URL}/api/v1/user" 2>/dev/null \
    | json_get login 2>/dev/null || true
}

team_id() { # <team name> -> id on stdout, '' if absent
  admin_send GET "/orgs/${ORG}/teams" ''
  python3 -c 'import json,sys; ids=[t["id"] for t in json.load(open(sys.argv[1])) if t["name"]==sys.argv[2]]; print(ids[0] if ids else "")' \
    "$RESP" "$1"
}

resp_id() { # id field of the last admin_send response
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$RESP"
}

# ---- wait for gitea ------------------------------------------------------------
gitea_healthz() { curl -fsS -o /dev/null "${GITEA_URL}/api/healthz" 2>/dev/null; }
log "waiting for gitea (via ${GITEA_URL}, up to ${GITEA_READY_TIMEOUT}s) ..."
retry_until "${GITEA_READY_TIMEOUT}" 2 "gitea healthy" gitea_healthz \
  || die "gitea not healthy after ${GITEA_READY_TIMEOUT}s — is the stack up? (make dev / make k8s-deploy)"
log "gitea is healthy"

# ---- admin user ----------------------------------------------------------------
# the chart's gitea provisions the admin (gitea.admin values); verify only
ADMIN_LOGIN=$(curl -sf -u "${ADMIN_USER}:${ARTEA_ADMIN_PASSWORD}" "${GITEA_URL}/api/v1/user" 2>/dev/null \
  | json_get login 2>/dev/null || true)
[ "${ADMIN_LOGIN}" = "${ADMIN_USER}" ] \
  || die "cannot authenticate as ${ADMIN_USER} — the chart must provision the admin user"
log "admin ${ADMIN_USER} present (chart-provisioned)"

# ---- admin PAT (reused from a previous run when still valid) -------------------
ADMIN_TOKEN=""
# shellcheck disable=SC1090
[ -f "${CRED_FILE}" ] && ADMIN_TOKEN="$(. "${CRED_FILE}"; echo "${ARTEA_ADMIN_TOKEN:-}")"
if [ "$(token_login "${ADMIN_TOKEN:-invalid}")" = "${ADMIN_USER}" ]; then
  log "reusing admin token from ${CRED_FILE}"
else
  log "minting admin token (scopes: all)"
  ADMIN_TOKEN=$(mint_token "${ADMIN_USER}" all)
fi

# ---- org (private: packages must not be world-readable) ------------------------
if [ "$(admin_code "/orgs/${ORG}")" = 200 ]; then
  log "org ${ORG} already exists"
else
  log "creating private org ${ORG}"
  admin_send POST /orgs "{\"username\":\"${ORG}\",\"visibility\":\"private\"}"
fi

# ---- policy repo ----------------------------------------------------------------
if [ "$(admin_code "/repos/${ORG}/${REPO}")" = 200 ]; then
  log "repo ${ORG}/${REPO} already exists"
else
  log "creating repo ${ORG}/${REPO}"
  admin_send POST "/orgs/${ORG}/repos" \
    "{\"name\":\"${REPO}\",\"private\":true,\"auto_init\":true,\"default_branch\":\"main\"}"
fi

seed_file() { # <local path> <path in repo>
  if [ "$(admin_code "/repos/${ORG}/${REPO}/contents/$2")" = 200 ]; then
    log "$2 already seeded"
    return
  fi
  local b64; b64=$(base64 < "$1" | tr -d '\n')
  admin_send POST "/repos/${ORG}/${REPO}/contents/$2" \
    "{\"content\":\"${b64}\",\"message\":\"chore: seed $2\"}"
  log "seeded $2"
}
ensure_generated_file() { # <local path> <path in repo> <marker>
  local b64 code sha cmp_status
  b64=$(base64 < "$1" | tr -d '\n')
  code=$(admin_code "/repos/${ORG}/${REPO}/contents/$2")
  case "$code" in
    200)
      set +e
      python3 - "$RESP" "$1" "$3" <<'PY'
import base64
import json
import sys

resp_path, desired_path, marker = sys.argv[1:]
data = json.load(open(resp_path))
current = base64.b64decode((data.get("content") or "").encode())
desired = open(desired_path, "rb").read()
if current == desired:
    sys.exit(0)
if marker.encode() in current:
    sys.exit(2)
sys.exit(3)
PY
      cmp_status=$?
      set -e
      case "$cmp_status" in
        0)
          log "$2 already up to date"
          ;;
        2)
          sha=$(json_get sha < "$RESP")
          admin_send PUT "/repos/${ORG}/${REPO}/contents/$2" \
            "{\"content\":\"${b64}\",\"sha\":\"${sha}\",\"message\":\"docs: update $2\"}"
          log "updated $2"
          ;;
        3)
          log "$2 exists and is not marked as generated; leaving it unchanged"
          ;;
        *)
          die "could not compare generated file $2"
          ;;
      esac
      ;;
    404)
      admin_send POST "/repos/${ORG}/${REPO}/contents/$2" \
        "{\"content\":\"${b64}\",\"message\":\"chore: seed $2\"}"
      log "seeded $2"
      ;;
    *)
      die "GET ${ORG}/${REPO}/contents/$2 -> HTTP $code: $(cat "$RESP")"
      ;;
  esac
}
sed_replacement_escape() { # <value> -> sed replacement-safe value
  printf '%s' "$1" | sed 's/[&|\\]/\\&/g'
}
render_client_setup() { # <output path>
  local public_url namespace
  public_url=$(sed_replacement_escape "${ARTEA_PUBLIC_URL}")
  namespace=$(sed_replacement_escape "${ARTEA_NAMESPACE}")
  sed \
    -e "s|__ARTEA_PUBLIC_URL__|${public_url}|g" \
    -e "s|__ARTEA_NAMESPACE__|${namespace}|g" \
    policy/CLIENT-SETUP.md.template > "$1"
}
# policy.toml is the canonical authoring source; policy-sync compiles it into
# the per-engine artifacts on every push.
seed_file policy/policy.toml policy.toml
render_client_setup "${CLIENT_SETUP_FILE}"
ensure_generated_file "${CLIENT_SETUP_FILE}" CLIENT-SETUP.md "Generated by Artea bootstrap"

# ---- push webhook -> policy-sync ------------------------------------------------
HOOK_URL="${POLICY_SYNC_HOOK_URL}"
admin_send GET "/repos/${ORG}/${REPO}/hooks" ''
if grep -q "${HOOK_URL}" "$RESP"; then
  log "policy webhook already wired"
else
  log "creating push webhook -> ${HOOK_URL}"
  admin_send POST "/repos/${ORG}/${REPO}/hooks" "{
    \"type\": \"gitea\", \"active\": true, \"events\": [\"push\"],
    \"config\": {\"url\": \"${HOOK_URL}\", \"content_type\": \"json\",
                  \"secret\": \"${POLICY_WEBHOOK_SECRET}\"}}"
fi

# ---- developers team (governance: devs are never org Owners) --------------------
# code write = PR branches on the policy repo; pulls write = open/review PRs;
# packages write = publish private-scope packages. No admin unit anywhere.
DEV_TEAM_ID=$(team_id developers)
if [ -n "${DEV_TEAM_ID}" ]; then
  log "team developers already exists (id ${DEV_TEAM_ID})"
else
  log "creating team developers (code+pulls+packages write, all repos)"
  admin_send POST "/orgs/${ORG}/teams" '{
    "name": "developers",
    "description": "Package developers: publish packages, change policy via PRs. Never Owners.",
    "permission": "write",
    "includes_all_repositories": true,
    "units_map": {"repo.code": "write", "repo.pulls": "write", "repo.packages": "write"}}'
  DEV_TEAM_ID=$(resp_id)
fi

# ---- demo user dev1 (developers member so it can read/write private packages) ----
if [ "$(admin_code /users/dev1)" = 200 ]; then
  log "user dev1 already exists"
else
  log "creating user dev1"
  create_user dev1 "${DEV1_PASSWORD}"
fi
admin_send PUT "/teams/${DEV_TEAM_ID}/members/dev1" ''
log "dev1 is a member of ${ORG} (developers team)"
# migration: older bootstraps put dev1 into Owners; removal is a no-op (204)
# when dev1 is not a member. Membership in developers must be ensured first so
# dev1 never drops out of the org entirely.
OWNERS_ID=$(team_id Owners)
admin_send DELETE "/teams/${OWNERS_ID}/members/dev1" ''
log "dev1 is not in Owners"

# ---- branch protection on the policy repo (S14: policy changes go through PRs) --
admin_send GET "/repos/${ORG}/${REPO}" ''
DEFAULT_BRANCH=$(json_get default_branch < "$RESP")
if [ "$(admin_code "/repos/${ORG}/${REPO}/branch_protections/${DEFAULT_BRANCH}")" = 200 ]; then
  log "branch protection on ${REPO}@${DEFAULT_BRANCH} already present"
else
  log "protecting ${REPO}@${DEFAULT_BRANCH} (>=1 approval; direct push only for ${ADMIN_USER})"
  # enable_push + whitelist = direct pushes blocked for everyone except the
  # allowlist; the e2e suite edits policy via the contents API as the admin user,
  # which goes through the same protected-branch check, so it must stay listed
  admin_send POST "/repos/${ORG}/${REPO}/branch_protections" "{
    \"rule_name\": \"${DEFAULT_BRANCH}\",
    \"enable_push\": true,
    \"enable_push_whitelist\": true,
    \"push_whitelist_usernames\": [\"${ADMIN_USER}\"],
    \"required_approvals\": 1,
    \"block_on_rejected_reviews\": true}"
fi

# ---- dev1 PAT --------------------------------------------------------------------
# write:package: publish+install and satisfies the gateway package-scope probe.
# read:user: required by Verdaccio's user check.
# read:organization: gateway org guard plus Verdaccio Artea team group mapping.
DEV1_SCOPES="write:package,read:user,read:organization"
DEV1_TOKEN=""
# shellcheck disable=SC1090
[ -f "${CRED_FILE}" ] && DEV1_TOKEN="$(. "${CRED_FILE}"; echo "${DEV1_TOKEN:-}")"
if [ "$(token_login "${DEV1_TOKEN:-invalid}")" = "dev1" ]; then
  log "reusing dev1 token from ${CRED_FILE}"
else
  log "minting dev1 token (scopes: ${DEV1_SCOPES})"
  DEV1_TOKEN=$(mint_token dev1 "${DEV1_SCOPES}")
fi

# ---- svc-policy service account (low-privilege reader of the policy repo) --------
SVC_USER=svc-policy
if [ "$(admin_code "/users/${SVC_USER}")" = 200 ]; then
  log "user ${SVC_USER} already exists"
else
  log "creating service user ${SVC_USER} (random password, PAT-only account)"
  create_user "${SVC_USER}" "$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
fi
READERS_ID=$(team_id policy-readers)
if [ -n "${READERS_ID}" ]; then
  log "team policy-readers already exists (id ${READERS_ID})"
else
  log "creating read-only team policy-readers (repo ${REPO} only)"
  admin_send POST "/orgs/${ORG}/teams" '{
    "name": "policy-readers",
    "description": "Read-only access to registry-policy for service accounts (policy-sync).",
    "permission": "read",
    "includes_all_repositories": false,
    "units_map": {"repo.code": "read"}}'
  READERS_ID=$(resp_id)
fi
admin_send PUT "/teams/${READERS_ID}/repos/${ORG}/${REPO}" ''
admin_send PUT "/teams/${READERS_ID}/members/${SVC_USER}" ''
log "${SVC_USER} has read-only access to ${ORG}/${REPO}"

# ---- policy-sync service PAT (svc-policy; delivered via the token sink) ----------
# Valid means: reads the raw policy file AND is low-privilege (pull-only on the
# repo). An admin-minted token from an older bootstrap reads fine but carries
# push+admin rights, so it fails the second check and gets rotated + revoked.
# the current token lives in the Secret
POLICY_SYNC_TOKEN="$("${KC[@]}" get secret "${SECRET_NAME}" -o "jsonpath={.data.${SECRET_KEY}}" 2>/dev/null \
  | base64 -d 2>/dev/null || true)"
policy_token_ok() {
  # Filename-agnostic readiness probe: the token must be able to read the policy
  # repo's contents. Do not assume any specific policy filename (the canonical
  # source is policy.toml, but legacy deployments only have the three legacy
  # files) — listing the repo contents succeeds whatever the policy layout is.
  curl -sf -o /dev/null -H "Authorization: token ${POLICY_SYNC_TOKEN:-invalid}" \
    "${GITEA_URL}/api/v1/repos/${ORG}/${REPO}/contents" || return 1
  curl -sf -H "Authorization: token ${POLICY_SYNC_TOKEN:-invalid}" \
      "${GITEA_URL}/api/v1/repos/${ORG}/${REPO}" 2>/dev/null \
    | python3 -c 'import json,sys; p=json.load(sys.stdin).get("permissions",{}); sys.exit(0 if p.get("pull") and not p.get("push") and not p.get("admin") else 1)' \
      2>/dev/null
}
if policy_token_ok; then
  log "current POLICY_SYNC_TOKEN is valid and low-privilege"
else
  log "minting ${SVC_USER} token (scopes: read:repository)"
  POLICY_SYNC_TOKEN=$(mint_token "${SVC_USER}" read:repository)
  log "patching secret ${SECRET_NAME} (key ${SECRET_KEY}) and restarting ${DEPLOYMENT_NAME}"
  PATCH_FILE=$(mktemp)
  token_b64=$(printf '%s' "${POLICY_SYNC_TOKEN}" | base64 | tr -d '\n')
  printf '{"data":{"%s":"%s"}}\n' "${SECRET_KEY}" "${token_b64}" > "${PATCH_FILE}"
  "${KC[@]}" patch secret "${SECRET_NAME}" --type=merge --patch-file "${PATCH_FILE}" >/dev/null
  rm -f "${PATCH_FILE}"
  PATCH_FILE=""
  "${KC[@]}" rollout restart "deployment/${DEPLOYMENT_NAME}" >/dev/null
  wait_deployment_rollout "${DEPLOYMENT_NAME}" \
    || die "deployment ${DEPLOYMENT_NAME} did not complete rollout within ${ROLLOUT_TIMEOUT}s"
  # migration: revoke superseded admin-minted policy tokens (recognizable by
  # their exact read:repository scope; the admin's own bootstrap PAT is 'all')
  curl -sf -u "${ADMIN_USER}:${ARTEA_ADMIN_PASSWORD}" \
      "${GITEA_URL}/api/v1/users/${ADMIN_USER}/tokens" \
    | python3 -c 'import json,sys
for t in json.load(sys.stdin):
    if t.get("scopes") == ["read:repository"]:
        print(t["id"])' \
    | while read -r tid; do
        log "revoking superseded admin-minted policy token (id ${tid})"
        curl -sf -o /dev/null -X DELETE -u "${ADMIN_USER}:${ARTEA_ADMIN_PASSWORD}" \
          "${GITEA_URL}/api/v1/users/${ADMIN_USER}/tokens/${tid}" || true
      done || log "WARN: could not enumerate admin tokens; old policy token not revoked"
fi

# ---- wait for the first successful policy sync ------------------------------------
policy_synced() {
  # the bootstrap Job runs in-cluster, so the service URL is reachable
  curl -sf --max-time 3 "${POLICY_SYNC_URL}/healthz" 2>/dev/null \
    | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("last_sync_ok") else 1)' 2>/dev/null
}
log "waiting for policy-sync to complete a sync ..."
retry_until 90 2 "policy-sync last_sync_ok" policy_synced \
  || die "policy-sync did not report a successful sync within 90s"
log "policy-sync reports last_sync_ok=true"

# ---- credentials for the e2e suite -------------------------------------------------
emit_credentials() {
  cat <<EOF
# generated by scripts/bootstrap.sh — gitignored, dev credentials only
GATEWAY_URL=${GATEWAY_URL}
ARTEA_NAMESPACE=${ARTEA_NAMESPACE}
POLICY_REPO=${POLICY_REPO}
ARTEA_ADMIN_USER=${ADMIN_USER}
ARTEA_ADMIN_PASSWORD=${ARTEA_ADMIN_PASSWORD}
ARTEA_ADMIN_TOKEN=${ADMIN_TOKEN}
DEV1_USER=dev1
DEV1_PASSWORD=${DEV1_PASSWORD}
DEV1_TOKEN=${DEV1_TOKEN}
POLICY_SYNC_TOKEN=${POLICY_SYNC_TOKEN}
EOF
}
write_credentials() { # <path> ; subshell so umask stays contained
  (mkdir -p "$(dirname "$1")" && umask 177 && emit_credentials > "$1")
}
if truthy "${EMIT_CREDENTIALS:-false}"; then
  # the harness/CI extracts this block from the Job logs (scripts/k8s-e2e.sh)
  echo "----- BEGIN ARTEA CREDENTIALS -----"
  emit_credentials
  echo "----- END ARTEA CREDENTIALS -----"
  if [ -n "${WRITE_CREDENTIALS_PATH:-}" ]; then
    if write_credentials "${CRED_FILE}" 2>/dev/null; then
      log "credentials also written to ${CRED_FILE}"
    else
      log "WARN: WRITE_CREDENTIALS_PATH=${CRED_FILE} not writable; rely on the log block"
    fi
  fi
else
  log "credential emission disabled (set EMIT_CREDENTIALS=true for e2e/dev)"
fi
log "bootstrap complete"
