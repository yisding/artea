#!/usr/bin/env bash
# Artea bootstrap (e2e scenario S1). Idempotent: safe to re-run at any time.
#
# Creates/ensures, in order: gitea secret files, admin `artea-admin` + PAT,
# private org `artea`, repo `artea/registry-policy` seeded from policy/,
# push webhook -> policy-sync, the `developers` team (code/pulls/packages
# write, no admin), demo user `dev1` (developers member, never Owners) + PAT,
# branch protection on the policy repo's default branch (PRs + >=1 approval;
# direct push only for artea-admin), the `svc-policy` service account in a
# read-only `policy-readers` team, and its policy-sync PAT (written back into
# .env, container recreated). Re-running migrates older stacks: dev1 is moved
# out of Owners and an admin-minted POLICY_SYNC_TOKEN is rotated to svc-policy
# and revoked. Resulting credentials land in e2e/tmp/credentials.env.
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

team_id() { # <team name> -> id on stdout, '' if absent
  admin_send GET "/orgs/${ORG}/teams" ''
  python3 -c 'import json,sys; ids=[t["id"] for t in json.load(open(sys.argv[1])) if t["name"]==sys.argv[2]]; print(ids[0] if ids else "")' \
    "$RESP" "$1"
}

resp_id() { # id field of the last admin_send response
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$RESP"
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

# ---- developers team (governance: devs are never org Owners) --------------------
# code write = PR branches on the policy repo; pulls write = open/review PRs;
# packages write = publish @artea packages. No admin unit anywhere.
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

# ---- demo user dev1 (developers member so it can read/write @artea packages) ----
if [ "$(admin_code /users/dev1)" = 200 ]; then
  log "user dev1 already exists"
else
  log "creating user dev1"
  gitea_cli admin user create --username dev1 --password "${DEV1_PASSWORD}" \
    --email dev1@localhost --must-change-password=false >/dev/null
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
DEFAULT_BRANCH=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["default_branch"])' "$RESP")
if [ "$(admin_code "/repos/${ORG}/${REPO}/branch_protections/${DEFAULT_BRANCH}")" = 200 ]; then
  log "branch protection on ${REPO}@${DEFAULT_BRANCH} already present"
else
  log "protecting ${REPO}@${DEFAULT_BRANCH} (>=1 approval; direct push only for ${ADMIN_USER})"
  # enable_push + whitelist = direct pushes blocked for everyone except the
  # allowlist; the e2e suite edits policy via the contents API as artea-admin,
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

# ---- svc-policy service account (low-privilege reader of the policy repo) --------
SVC_USER=svc-policy
if [ "$(admin_code "/users/${SVC_USER}")" = 200 ]; then
  log "user ${SVC_USER} already exists"
else
  log "creating service user ${SVC_USER} (random password, PAT-only account)"
  gitea_cli admin user create --username "${SVC_USER}" --random-password \
    --email "${SVC_USER}@localhost" --must-change-password=false >/dev/null
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

# ---- policy-sync service PAT (svc-policy; lands in .env, container recreated) ----
# Valid means: reads the raw policy file AND is low-privilege (pull-only on the
# repo). An admin-minted token from an older bootstrap reads fine but carries
# push+admin rights, so it fails the second check and gets rotated + revoked.
policy_token_ok() {
  curl -sf -o /dev/null -H "Authorization: token ${POLICY_SYNC_TOKEN:-invalid}" \
    "${GATEWAY_URL}/api/v1/repos/${ORG}/${REPO}/raw/npm-rules.yaml" || return 1
  curl -sf -H "Authorization: token ${POLICY_SYNC_TOKEN:-invalid}" \
      "${GATEWAY_URL}/api/v1/repos/${ORG}/${REPO}" 2>/dev/null \
    | python3 -c 'import json,sys; p=json.load(sys.stdin).get("permissions",{}); sys.exit(0 if p.get("pull") and not p.get("push") and not p.get("admin") else 1)' \
      2>/dev/null
}
if policy_token_ok; then
  log "POLICY_SYNC_TOKEN in .env is valid and low-privilege"
else
  log "minting ${SVC_USER} token (scopes: read:repository) and updating .env"
  POLICY_SYNC_TOKEN=$(mint_token "${SVC_USER}" read:repository)
  sed -i.bak "s|^POLICY_SYNC_TOKEN=.*|POLICY_SYNC_TOKEN=${POLICY_SYNC_TOKEN}|" .env && rm -f .env.bak
  log "recreating policy-sync with the new token"
  # --build: also picks up image changes (e.g. the non-root migration) when
  # bootstrap is re-run without a prior `make up`
  docker compose up -d --build --wait policy-sync >/dev/null
  # migration: revoke superseded admin-minted policy tokens (recognizable by
  # their exact read:repository scope; the admin's own bootstrap PAT is 'all')
  curl -sf -u "${ADMIN_USER}:${ARTEA_ADMIN_PASSWORD}" \
      "${GATEWAY_URL}/api/v1/users/${ADMIN_USER}/tokens" \
    | python3 -c 'import json,sys
for t in json.load(sys.stdin):
    if t.get("scopes") == ["read:repository"]:
        print(t["id"])' \
    | while read -r tid; do
        log "revoking superseded admin-minted policy token (id ${tid})"
        curl -sf -o /dev/null -X DELETE -u "${ADMIN_USER}:${ARTEA_ADMIN_PASSWORD}" \
          "${GATEWAY_URL}/api/v1/users/${ADMIN_USER}/tokens/${tid}" || true
      done || log "WARN: could not enumerate admin tokens; old policy token not revoked"
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
