#!/usr/bin/env sh
# Generate the secret files referenced by gitea/app.ini.template (SECRET_KEY_URI /
# INTERNAL_TOKEN_URI). Run before the first `docker compose up` (wired into
# `make bootstrap`). Idempotent: existing non-empty files are kept — regenerating
# SECRET_KEY invalidates sessions/2FA secrets, so never overwrite silently.
set -eu

dir=$(cd "$(dirname "$0")/.." && pwd)/secrets
mkdir -p "$dir"

gen() {
	name=$1
	bytes=$2
	if [ -s "$dir/$name" ]; then
		echo "kept     gitea/secrets/$name"
	else
		openssl rand -hex "$bytes" > "$dir/$name"
		# 0644 so the container's git user (uid 1000) can read the bind mount on
		# Linux hosts too; gitea/secrets/ is gitignored and dev-only.
		chmod 644 "$dir/$name"
		echo "created  gitea/secrets/$name"
	fi
}

gen secret_key 32
gen internal_token 64

# oauth2.JWT_SECRET (also Gitea's general token signing secret): must be
# RawURL-base64 of exactly 32 bytes, otherwise Gitea regenerates it and tries
# to write it back into the read-only rendered app.ini and fatals on boot.
if [ -s "$dir/jwt_secret" ]; then
	echo "kept     gitea/secrets/jwt_secret"
else
	openssl rand -base64 32 | tr '+/' '-_' | tr -d '=' > "$dir/jwt_secret"
	chmod 644 "$dir/jwt_secret"
	echo "created  gitea/secrets/jwt_secret"
fi
