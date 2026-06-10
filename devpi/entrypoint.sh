#!/usr/bin/env bash
# Artea devpi entrypoint: idempotent init, server start, ensure root/constrained.
# devpi is ONLY a disposable pull-through cache of pypi.org with constraints
# filtering. Private packages live in Gitea; auth is enforced by the gateway.
set -euo pipefail

SERVERDIR="${DEVPISERVER_SERVERDIR:-/devpi/server}"
PORT="${DEVPI_PORT:-3141}"
OUTSIDE_URL="${DEVPI_OUTSIDE_URL:-http://localhost:8080}"
STARTUP_TIMEOUT="${DEVPI_STARTUP_TIMEOUT:-60}"
LOCAL_URL="http://127.0.0.1:${PORT}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() { echo "[devpi-entrypoint] $*" >&2; }

if [ -z "${DEVPI_ROOT_PASSWORD:-}" ]; then
  log "ERROR: DEVPI_ROOT_PASSWORD must be set (see .env)"
  exit 1
fi

# first boot only: devpi-init refuses an initialized dir; .serverversion is its marker
if [ ! -f "${SERVERDIR}/.serverversion" ]; then
  log "server dir ${SERVERDIR} not initialized, running devpi-init"
  devpi-init --serverdir "${SERVERDIR}" --root-passwd "${DEVPI_ROOT_PASSWORD}"
else
  log "server dir ${SERVERDIR} already initialized, skipping devpi-init"
fi

# --outside-url + --absolute-urls: simple-page file links become absolute URLs
#   under the gateway origin (/root/pypi/+f/...), per the architecture contract;
#   without --absolute-urls devpi emits relative hrefs that break behind the
#   gateway's /pypi/simple/ -> /root/constrained/+simple/ path translation
# --restrict-modify root: defense in depth, devpi otherwise allows anonymous signup
log "starting devpi-server on 0.0.0.0:${PORT} (outside-url ${OUTSIDE_URL})"
devpi-server \
  --host 0.0.0.0 \
  --port "${PORT}" \
  --serverdir "${SERVERDIR}" \
  --outside-url "${OUTSIDE_URL}" \
  --absolute-urls \
  --restrict-modify root &
SERVER_PID=$!

shutdown() { kill -TERM "${SERVER_PID}" 2>/dev/null || true; }
trap shutdown TERM INT

probe() {
  python3 -c "import urllib.request; urllib.request.urlopen('${LOCAL_URL}/+status', timeout=2)" 2>/dev/null
}

deadline=$(( $(date +%s) + STARTUP_TIMEOUT ))
until probe; do
  if ! kill -0 "${SERVER_PID}" 2>/dev/null; then
    log "ERROR: devpi-server exited during startup"
    exit 1
  fi
  if [ "$(date +%s)" -ge "${deadline}" ]; then
    log "ERROR: devpi-server not ready after ${STARTUP_TIMEOUT}s"
    shutdown
    exit 1
  fi
  sleep 0.5
done
log "devpi-server is up"

# ensure the filtered index exists (the gateway's pypi 404-fallback target);
# a fresh index is seeded fail-closed ('*' = block all) and an existing one is
# left alone — its constraints are owned by policy-sync (see ensure_index.py).
# Uses the JSON API, not devpi-client: --outside-url rewrites client URLs (README)
python3 "${SCRIPT_DIR}/ensure_index.py" "${LOCAL_URL}"

# test/maintenance hook: init everything, then exit instead of serving
if [ "${DEVPI_ONESHOT:-0}" = "1" ]; then
  log "DEVPI_ONESHOT=1: init complete, stopping server"
  shutdown
  wait "${SERVER_PID}" 2>/dev/null || true
  exit 0
fi

# keep devpi-server in the foreground; first wait may be interrupted by the trap
set +e
wait "${SERVER_PID}"
code=$?
if [ "${code}" -gt 128 ]; then
  wait "${SERVER_PID}" 2>/dev/null
  code2=$?
  [ "${code2}" -ne 127 ] && code=${code2}
fi
exit "${code}"
