#!/bin/sh
# Drop root before running the service. The container starts as root only to
# repair /policy ownership: the shared `policy-data` named volume may predate
# the non-root image (docker seeds volume ownership from the image only on
# first creation). Verdaccio (different uid) just reads /policy, so the dir
# stays 0755 and policy-sync writes files as 0644.
set -eu

POLICY_DIR="${POLICY_DIR:-/policy}"

# K8s/HTTP-only mode mounts no /policy volume: nothing to repair, the service
# serves the policy over HTTP instead (POLICY_FILE_PATH="")
if [ "$(id -u)" = "0" ]; then
  if [ -d "${POLICY_DIR}" ]; then
    chown -R policysync:policysync "${POLICY_DIR}"
    chmod 755 "${POLICY_DIR}"
  fi
  exec setpriv --reuid policysync --regid policysync --init-groups "$@"
fi

exec "$@"
