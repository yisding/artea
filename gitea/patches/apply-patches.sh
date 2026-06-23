#!/usr/bin/env sh
# Apply the gitea/patches/series queue onto an upstream Gitea source checkout.
# Usage: apply-patches.sh [--check] <upstream-checkout-dir>
#   --check  dry-run only; verifies every patch applies cleanly, modifies nothing.
# The checkout must be at the SOURCE_TAG pinned in gitea/UPSTREAM.
set -eu

usage() {
	echo "usage: $0 [--check] <upstream-checkout-dir>" >&2
	exit 2
}

check=0
if [ "${1:-}" = "--check" ]; then
	check=1
	shift
fi
[ $# -eq 1 ] || usage
target=$1

patch_dir=$(cd "$(dirname "$0")" && pwd)
series="$patch_dir/series"

[ -d "$target" ] || { echo "error: not a directory: $target" >&2; exit 1; }
[ -f "$series" ] || { echo "error: missing $series" >&2; exit 1; }

count=0
while IFS= read -r line; do
	# skip blanks and '#' comments
	case "$line" in ''|\#*) continue ;; esac
	p="$patch_dir/$line"
	[ -f "$p" ] || { echo "error: patch listed in series but missing: $p" >&2; exit 1; }
	echo "==> $line"
	# git apply (not `patch -p1`): the patches are git-format and may add files,
	# which BSD `patch` (macOS) cannot create from a /dev/null diff. git apply
	# handles new files/renames identically on macOS and Linux. The target must be
	# a git checkout (it always is — see the bump procedure in gitea/UPSTREAM).
	if [ "$check" -eq 1 ]; then
		git -C "$target" apply --check "$p"
	else
		git -C "$target" apply "$p"
	fi
	count=$((count + 1))
done < "$series"

if [ "$check" -eq 1 ]; then
	echo "OK: $count patch(es) apply cleanly on $target (dry run)"
else
	echo "OK: $count patch(es) applied on $target"
fi
