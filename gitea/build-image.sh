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
#   --push  build a multi-arch (linux/amd64,linux/arm64) manifest with buildx and
#           push it straight to the registry — the published image must run on
#           both amd64 and arm64 (e.g. Graviton) nodes. Override the platform list
#           with $ARTEA_GITEA_PLATFORMS. Without --push the build is a plain,
#           single-arch (host) `docker build` loaded into the local docker image
#           store (colima k3s shares it): a multi-arch build cannot be --loaded
#           locally, and local dev only needs the host arch.
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

if [ "$push" -eq 1 ]; then
	# Multi-arch manifest pushed straight to the registry: a multi-platform build
	# cannot be --loaded into the local docker store, and the published image must
	# run on both amd64 and arm64 (e.g. Graviton) nodes. Needs a buildx builder;
	# any non-host arch additionally needs QEMU (slow for Gitea's CGO build — CI
	# avoids it by setting ARTEA_GITEA_PLATFORMS to one arch per native runner).
	platforms="${ARTEA_GITEA_PLATFORMS:-linux/amd64,linux/arm64}"
	echo "==> building + pushing $IMAGE:$TAG ($platforms, rootless) from patched source"
	docker buildx build \
		-f "$tmp/src/Dockerfile.rootless" \
		--platform "$platforms" \
		--build-arg GITEA_VERSION="${VERSION}-pkce" \
		-t "$IMAGE:$TAG" \
		--push \
		"$tmp/src"
else
	# Local single-arch build into the host docker image store (colima k3s shares
	# it); local dev only needs the host arch and a multi-arch build can't --load.
	echo "==> building $IMAGE:$TAG (host arch, rootless) from patched source"
	docker build \
		-f "$tmp/src/Dockerfile.rootless" \
		-t "$IMAGE:$TAG" \
		--build-arg GITEA_VERSION="${VERSION}-pkce" \
		"$tmp/src"
fi

echo "built: $IMAGE:$TAG"
