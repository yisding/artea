# Shared credentials-load preamble for the Artea e2e suite, sourced by both
# scripts/smoke.sh and e2e/run.sh so they locate credentials and resolve the
# gateway/namespace identically. Assumes cwd = repo root (both callers cd there
# first). Each caller keeps its own missing-creds error and exit (smoke is
# deliberately standalone; run.sh uses lib.sh's die), then calls load_credentials.
# shellcheck shell=bash

resolve_credentials_file() { # default + normalize CREDENTIALS_FILE; caller then checks it exists
  CREDENTIALS_FILE="${CREDENTIALS_FILE:-e2e/tmp/credentials.env}"
  case "${CREDENTIALS_FILE}" in /*) ;; *) CREDENTIALS_FILE="./${CREDENTIALS_FILE}" ;; esac
}

load_credentials() { # source the (already-verified) credentials file, resolve shared globals
  # shellcheck disable=SC1090
  source "${CREDENTIALS_FILE}"
  # explicit BASE_URL beats the GATEWAY_URL recorded at bootstrap time
  GATEWAY_URL="${BASE_URL:-${GATEWAY_URL:-http://localhost:8080}}"
  ARTEA_NAMESPACE="${ARTEA_NAMESPACE:-artea}"
}
