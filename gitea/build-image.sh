#!/usr/bin/env bash
# Reproducible build of the PATCHED rootless Gitea image (ADR-0009).
#
# Clones stock upstream Gitea at the SOURCE_TAG pinned in gitea/UPSTREAM,
# applies the gitea/patches/ queue (the PKCE patch — see ADR-0009) via
# gitea/patches/apply-patches.sh (git apply), and builds the result with
# Gitea's own Dockerfile.rootless. Output: ghcr.io/yisding/artea-gitea:<tag>
# (override the image name with $ARTEA_GITEA_IMAGE).
#
# Usage: gitea/build-image.sh [TAG] [--push]      (TAG defaults to "local")
#
# Deletion path: this whole build exists only because stock Gitea cannot send a
# PKCE code_challenge on OIDC login sources. Once upstream ships client-side
# PKCE (go-gitea/gitea#34747 / #21376), drop gitea/patches/, delete this script,
# and return to the stock image — see ADR-0009.
set -euo pipefail

# repo root = parent of this script's directory (gitea/)
repo_root=$(cd "$(dirname "$0")/.." && pwd)
cd "$repo_root"

TAG=local
push=0
for arg in "$@"; do
	case "$arg" in
		--push) push=1 ;;
		-*) echo "error: unknown flag: $arg" >&2; exit 2 ;;
		*) TAG=$arg ;;
	esac
done

upstream="$repo_root/gitea/UPSTREAM"
[ -f "$upstream" ] || { echo "error: missing $upstream" >&2; exit 1; }
VERSION=$(grep -E '^VERSION=' "$upstream" | head -n1 | sed 's/^VERSION=//')
SOURCE_TAG=$(grep -E '^SOURCE_TAG=' "$upstream" | head -n1 | sed 's/^SOURCE_TAG=//')
[ -n "$VERSION" ] || { echo "error: VERSION not found in $upstream" >&2; exit 1; }
[ -n "$SOURCE_TAG" ] || { echo "error: SOURCE_TAG not found in $upstream" >&2; exit 1; }

IMAGE="${ARTEA_GITEA_IMAGE:-ghcr.io/yisding/artea-gitea}"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

echo "==> cloning go-gitea/gitea at $SOURCE_TAG"
git clone --quiet --depth 1 --branch "$SOURCE_TAG" \
	https://github.com/go-gitea/gitea.git "$tmp/src"

echo "==> applying gitea/patches/ queue"
"$repo_root/gitea/patches/apply-patches.sh" "$tmp/src"

echo "==> building $IMAGE:$TAG (rootless) from patched source"
docker build \
	-f "$tmp/src/Dockerfile.rootless" \
	-t "$IMAGE:$TAG" \
	--build-arg GITEA_VERSION="${VERSION}-pkce" \
	"$tmp/src"

if [ "$push" -eq 1 ]; then
	echo "==> pushing $IMAGE:$TAG"
	docker push "$IMAGE:$TAG"
fi

echo "built: $IMAGE:$TAG"
