"""Idempotently ensure the root/constrained index exists on a local devpi.

A *freshly created* index is seeded with the `*` constraint — the
block-everything sentinel — so a wiped devpi-data volume is fail-closed
(serves nothing from the mirror) until policy-sync's next successful sync
replaces the constraints with the real policy (R3, e2e S15).
An existing index is never modified here: its constraints are owned by
policy-sync.

Talks to devpi's JSON API directly (stdlib only). devpi-client cannot be used
for this: with --outside-url set, the server's /+api response rewrites the
client's target URL to the gateway origin, which is not reachable (and not
devpi) from inside this container.

Usage: python3 ensure_index.py http://127.0.0.1:3141
Auth:  root password from $DEVPI_ROOT_PASSWORD (devpi accepts HTTP Basic).
Exit:  0 = index exists or was created; non-zero = error (including an
       existing root/constrained with the wrong type, which would silently
       serve an unfiltered mirror and violate R3).
"""

import base64
import json
import os
import sys
import urllib.error
import urllib.request

INDEX = "root/constrained"
KVDICT = {"type": "constrained", "bases": "root/pypi", "constraints": ["*"], "min_upstream_age": "P0D"}


def log(msg):
    print(f"[ensure-index] {msg}", file=sys.stderr)


def has_expected_base(value):
    # Parallel to _has_expected_base in policy-sync/policy_sync/devpi.py: the two
    # images cannot import each other, so this guards the same R3 base invariant
    # in both. Keep the two copies in sync.
    return value == "root/pypi" or value == ["root/pypi"]


def main():
    base = sys.argv[1].rstrip("/")
    url = f"{base}/{INDEX}"

    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            config = json.load(resp).get("result", {})
        actual = config.get("type")
        if actual != "constrained":
            log(f"ERROR: {INDEX} exists with type={actual!r}, expected 'constrained'")
            return 1
        bases = config.get("bases")
        if not has_expected_base(bases):
            log(f"ERROR: {INDEX} exists with bases={bases!r}, expected root/pypi")
            return 1
        log(f"index {INDEX} already exists")
        return 0
    except urllib.error.HTTPError as e:
        if e.code != 404:
            log(f"ERROR: GET {url} -> {e.code}")
            return 1

    auth = base64.b64encode(
        f"root:{os.environ['DEVPI_ROOT_PASSWORD']}".encode()
    ).decode()
    req = urllib.request.Request(
        url,
        data=json.dumps(KVDICT).encode(),
        method="PUT",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Basic {auth}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
    except urllib.error.HTTPError as e:
        log(f"ERROR: PUT {url} -> {e.code}: {e.read().decode(errors='replace')[:500]}")
        return 1
    log(f"created index {INDEX} (type=constrained, bases=root/pypi, "
        "constraints=['*'], min_upstream_age=P0D fail-closed until policy-sync syncs)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
