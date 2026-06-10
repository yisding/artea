#!/usr/bin/env bash
# Artea bootstrap (e2e scenario S1). Idempotent: safe to re-run at any time.
#
# Creates/ensures, in order: gitea secret files, admin `artea-admin` + PAT,
# private org `artea`, repo `artea/registry-policy` seeded from policy/,
# push webhook -> policy-sync, demo user `dev1` (org member) + package PAT,
# and the policy-sync service PAT (written back into .env, container
# recreated). Resulting credentials land in e2e/tmp/credentials.env.
set -euo pipefail
cd "$(dirname "$0")/.."

GATEWAY_URL="${GATEWAY_URL:-http://localhost:8080}"
ADMIN_USER=artea-admin
ORG=artea
REPO=registry-policy
CRED_FILE=e2e/tmp/credentials.env
RESP="$(mktemp)"
trap 'rm -f "$RESP"' EXIT

log() { echo "[bootstrap] $*"; }
die() { echo "[bootstrap] ERROR: $*" >&2; exit 1; }

[ -f .env ] || die ".env is missing — cp .env.example .env and change the secrets"
set -a; source ./.env; set +a
: "${ARTEA_ADMIN_PASSWORD:?must be set in .env}"
: "${DEV1_PASSWORD:?must be set in .env}"
: "${POLICY_WEBHOOK_SECRET:?must be set in .env}"

./gitea/scripts/gen-secrets.sh

# ---- helpers -----------------------------------------------------------------
gitea_cli() { docker compose exec -T -u git gitea gitea "$@"; }

# all CLI-minted tokens get unique names; gitea refuses duplicate token names
mint_token() { # <user> <scopes>  -> raw token on stdout
  gitea_cli admin user generate-access-token \
    --username "$1" --scopes "$2" --token-name "bootstrap-$(date +%s)-$RANDOM" --raw | tr -d '[:space:]'
}

admin_code() { # <api path> -> http status code
  curl -sS -o "$RESP" -w '%{http_code}' -H "Authorization: token ${ADMIN_TOKEN}" "${GATEWAY_URL}/api/v1$1"
}

admin_send() { # <method> <api path> <json body or ''> ; dies on non-2xx
  local code
  code=$(curl -sS -o "$RESP" -w '%{http_code}' -X "$1" \
    -H "Authorization: token ${ADMIN_TOKEN}" -H 'Content-Type: application/json' \
    ${3:+-d "$3"} "${GATEWAY_URL}/api/v1$2")
  case "$code" in 2*) ;; *) die "$1 $2 -> HTTP $code: $(cat "$RESP")";; esac
}

token_login() { # <token> -> login name of the token's user, '' if invalid
  curl -sf -H "Authorization: token $1" "${GATEWAY_URL}/api/v1/user" 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("login",""))' 2>/dev/null || true
}

# ---- wait for gitea through the gateway ----------------------------------------
log "waiting for gitea (via ${GATEWAY_URL}) ..."
for i in $(seq 1 60); do
  curl -fsS -o /dev/null "${GATEWAY_URL}/api/healthz" 2>/dev/null && break
  [ "$i" -eq 60 ] && die "gitea not healthy after 120s — is the stack up? (make up)"
  sleep 2
done
log "gitea is healthy"

# ---- admin user ----------------------------------------------------------------
if gitea_cli admin user list --admin | awk '{print $2}' | grep -qx "${ADMIN_USER}"; then
  log "admin ${ADMIN_USER} already exists"
else
  log "creating admin ${ADMIN_USER}"
  gitea_cli admin user create --admin --username "${ADMIN_USER}" \
    --password "${ARTEA_ADMIN_PASSWORD}" --email "${ADMIN_USER}@localhost" \
    --must-change-password=false >/dev/null
fi

# ---- admin PAT (reused from a previous run when still valid) -------------------
mkdir -p e2e/tmp
ADMIN_TOKEN=""
[ -f "${CRED_FILE}" ] && ADMIN_TOKEN="$(. "./${CRED_FILE}"; echo "${ARTEA_ADMIN_TOKEN:-}")"
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
seed_file policy/npm-rules.yaml npm-rules.yaml
seed_file policy/pypi-constraints.txt pypi-constraints.txt

# ---- push webhook -> policy-sync ------------------------------------------------
HOOK_URL="http://policy-sync:8920/hooks/policy"
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

# ---- demo user dev1 (org member so it can read/write @artea packages) -----------
if [ "$(admin_code /users/dev1)" = 200 ]; then
  log "user dev1 already exists"
else
  log "creating user dev1"
  gitea_cli admin user create --username dev1 --password "${DEV1_PASSWORD}" \
    --email dev1@localhost --must-change-password=false >/dev/null
fi
OWNERS_ID=$(admin_code "/orgs/${ORG}/teams" >/dev/null; python3 -c \
  'import json,sys; print([t["id"] for t in json.load(open(sys.argv[1])) if t["name"]=="Owners"][0])' "$RESP")
admin_send PUT "/teams/${OWNERS_ID}/members/dev1" ''
log "dev1 is a member of ${ORG} (Owners team)"

# ---- dev1 PAT --------------------------------------------------------------------
# write:package: publish+install. read:user: required by the gateway auth_request
# guard (GET /api/v1/user). read:organization: verdaccio org->group mapping.
DEV1_SCOPES="write:package,read:user,read:organization"
DEV1_TOKEN=""
[ -f "${CRED_FILE}" ] && DEV1_TOKEN="$(. "./${CRED_FILE}"; echo "${DEV1_TOKEN:-}")"
if [ "$(token_login "${DEV1_TOKEN:-invalid}")" = "dev1" ]; then
  log "reusing dev1 token from ${CRED_FILE}"
else
  log "minting dev1 token (scopes: ${DEV1_SCOPES})"
  DEV1_TOKEN=$(mint_token dev1 "${DEV1_SCOPES}")
fi

# ---- policy-sync service PAT (lands in .env, container recreated) ----------------
policy_token_ok() {
  curl -sf -o /dev/null -H "Authorization: token ${POLICY_SYNC_TOKEN:-invalid}" \
    "${GATEWAY_URL}/api/v1/repos/${ORG}/${REPO}/raw/npm-rules.yaml"
}
if policy_token_ok; then
  log "POLICY_SYNC_TOKEN in .env is valid"
else
  log "minting policy-sync token (scopes: read:repository) and updating .env"
  POLICY_SYNC_TOKEN=$(mint_token "${ADMIN_USER}" read:repository)
  sed -i.bak "s|^POLICY_SYNC_TOKEN=.*|POLICY_SYNC_TOKEN=${POLICY_SYNC_TOKEN}|" .env && rm -f .env.bak
  log "recreating policy-sync with the new token"
  docker compose up -d --wait policy-sync >/dev/null
fi

# ---- wait for the first successful policy sync ------------------------------------
log "waiting for policy-sync to complete a sync ..."
for i in $(seq 1 45); do
  if docker compose exec -T policy-sync python -c \
    "import json,sys,urllib.request; d=json.load(urllib.request.urlopen('http://127.0.0.1:8920/healthz', timeout=3)); sys.exit(0 if d.get('last_sync_ok') else 1)" \
    2>/dev/null; then
    log "policy-sync reports last_sync_ok=true"
    break
  fi
  [ "$i" -eq 45 ] && die "policy-sync did not report a successful sync within 90s"
  sleep 2
done

# ---- credentials for the e2e suite -------------------------------------------------
umask 177
cat > "${CRED_FILE}" <<EOF
# generated by scripts/bootstrap.sh — gitignored, dev credentials only
GATEWAY_URL=${GATEWAY_URL}
ARTEA_ADMIN_USER=${ADMIN_USER}
ARTEA_ADMIN_PASSWORD=${ARTEA_ADMIN_PASSWORD}
ARTEA_ADMIN_TOKEN=${ADMIN_TOKEN}
DEV1_USER=dev1
DEV1_PASSWORD=${DEV1_PASSWORD}
DEV1_TOKEN=${DEV1_TOKEN}
POLICY_SYNC_TOKEN=${POLICY_SYNC_TOKEN}
EOF
log "credentials written to ${CRED_FILE}"
log "bootstrap complete"
