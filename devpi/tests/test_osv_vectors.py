"""The devpi plugin parses the same OSV decision wire shape policy-sync emits and
the npm filter consumes — driven by the shared cross-language vector
(docs/policy-spec/osv-decision-vectors.json).
"""

import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "artea_devpi_policy" / "src"))

from artea_devpi_policy.main import query_osv_blocked_versions  # noqa: E402

_VECTORS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "docs", "policy-spec", "osv-decision-vectors.json"
)
with open(_VECTORS_PATH) as f:
    VECTORS = json.load(f)


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self, *args):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_query_osv_parses_shared_wire_shape(monkeypatch):
    body = json.dumps(VECTORS["response"]).encode()
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(body))

    request = VECTORS["request"]
    blocked = query_osv_blocked_versions(
        "http://policy-sync.example/osv/querybatch", request["name"], request["versions"]
    )

    assert blocked == set(VECTORS["expected"]["blockedVersions"])
