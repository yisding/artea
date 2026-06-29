import json
import threading
import urllib.request

import pytest

from policy_sync.osv import OsvClient
from policy_sync.policy_model import parse_policy
from policy_sync.server import PolicySyncHTTPServer, SyncState
from policy_sync.store import ParsedPolicyStore, PolicyStore
from tests._stub import StubServer, _StubHandler, reply
from tests.conftest import TEST_SECRET


class MockOsv(StubServer):
    def __init__(self):
        self.malicious: dict[str, list[str]] = {}
        self.vulnerable: set[str] = set()
        self.fail = False
        super().__init__()

    def _build_handler(self):
        mock = self

        class Handler(_StubHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                mock.requests.append(json.loads(body))  # MockOsv captures parsed payload
                if mock.fail:
                    self.send_error(500)
                    return
                if self.path != "/v1/querybatch":
                    self.send_error(404)
                    return
                queries = json.loads(body).get("queries", [])
                results = []
                for query in queries:
                    version = query["version"]
                    vulns = [{"id": mid, "modified": "2026-01-01T00:00:00Z"} for mid in mock.malicious.get(version, [])]
                    if version in mock.vulnerable:
                        vulns.append({"id": "GHSA-xxxx-yyyy-zzzz", "modified": "2026-01-01T00:00:00Z"})
                    results.append({"vulns": vulns} if vulns else {})
                reply(self, 200, json.dumps({"results": results}).encode())

        return Handler


def policy(text: str = ""):
    return parse_policy((
        "schema = 1\n"
        "[osv]\n"
        "malicious_packages = true\n"
        + text
    ).encode())


def test_osv_client_blocks_only_malicious_package_ids():
    osv = MockOsv()
    osv.malicious["1.0.0"] = ["MAL-2026-1"]
    osv.vulnerable.add("2.0.0")
    osv.start()
    try:
        client = OsvClient(api_url=osv.url)
        result = client.decide(policy(), "npm", "left-pad", ["1.0.0", "2.0.0", "3.0.0"])
    finally:
        osv.stop()

    assert result.status == "ok"
    assert [(v.version, v.blocked, v.ids) for v in result.verdicts] == [
        ("1.0.0", True, ("MAL-2026-1",)),
        ("2.0.0", False, ()),
        ("3.0.0", False, ()),
    ]


def test_curated_allow_overrides_osv_malicious_hit():
    osv = MockOsv()
    osv.malicious["1.0.0"] = ["MAL-2026-1"]
    osv.start()
    try:
        client = OsvClient(api_url=osv.url)
        result = client.decide(policy(
            '[[rules]]\n'
            'ecosystem = "npm"\n'
            'name = "left-pad"\n'
            'versions = "1.0.0"\n'
            'action = "allow"\n'
        ), "npm", "left-pad", ["1.0.0", "2.0.0"])
    finally:
        osv.stop()

    assert [(v.version, v.blocked) for v in result.verdicts] == [("1.0.0", False), ("2.0.0", False)]
    queried_versions = [q["version"] for q in osv.requests[0]["queries"]]
    assert queried_versions == ["2.0.0"]


def test_curated_pypi_exact_allow_overrides_with_release_equality():
    # A curated `==1.0` allow must override a MAL verdict on an upstream `1.0.0`:
    # PEP 440 treats them equal, so the version is allowed and never queried.
    osv = MockOsv()
    osv.malicious["1.0.0"] = ["MAL-2026-1"]
    osv.start()
    try:
        client = OsvClient(api_url=osv.url)
        result = client.decide(policy(
            '[[rules]]\n'
            'ecosystem = "pypi"\n'
            'name = "six"\n'
            'versions = "==1.0"\n'
            'action = "allow"\n'
        ), "pypi", "six", ["1.0.0", "2.0.0"])
    finally:
        osv.stop()

    assert [(v.version, v.blocked) for v in result.verdicts] == [("1.0.0", False), ("2.0.0", False)]
    queried_versions = [q["version"] for q in osv.requests[0]["queries"]]
    assert queried_versions == ["2.0.0"]


def test_curated_pypi_exact_allow_ignores_oversized_candidate_version():
    osv = MockOsv()
    huge_version = "1." + ("9" * 4301)
    osv.start()
    try:
        client = OsvClient(api_url=osv.url)
        result = client.decide(policy(
            '[[rules]]\n'
            'ecosystem = "pypi"\n'
            'name = "six"\n'
            'versions = "==1.0"\n'
            'action = "allow"\n'
        ), "pypi", "six", [huge_version])
    finally:
        osv.stop()

    assert result.status == "ok"
    assert [(v.version, v.blocked) for v in result.verdicts] == [(huge_version, False)]
    assert [q["version"] for q in osv.requests[0]["queries"]] == [huge_version]


def test_decide_accepts_osv_pypi_ecosystem_alias():
    # The OSV-cased "PyPI" alias must be accepted just like the internal "pypi"
    # and resolve to the same adapter/verdicts (single accepted-input mapping).
    osv = MockOsv()
    osv.malicious["1.0.0"] = ["MAL-2026-1"]
    osv.start()
    try:
        client = OsvClient(api_url=osv.url)
        result = client.decide(policy(), "PyPI", "six", ["1.0.0", "2.0.0"])
    finally:
        osv.stop()

    assert result.status == "ok"
    assert [(v.version, v.blocked) for v in result.verdicts] == [("1.0.0", True), ("2.0.0", False)]
    # the querybatch must use OSV's "PyPI" ecosystem casing
    assert osv.requests[0]["queries"][0]["package"]["ecosystem"] == "PyPI"


def test_decide_rejects_unknown_ecosystem():
    from policy_sync.policy_model import PolicyError

    client = OsvClient(api_url="http://127.0.0.1:1")  # never contacted
    with pytest.raises(PolicyError):
        client.decide(policy(), "cargo", "serde", ["1.0.0"])


def test_osv_outage_fails_open_but_uses_cached_malicious_verdict():
    osv = MockOsv()
    osv.malicious["1.0.0"] = ["MAL-2026-1"]
    osv.start()
    try:
        client = OsvClient(api_url=osv.url)
        first = client.decide(policy(), "npm", "left-pad", ["1.0.0"])
        osv.fail = True
        second = client.decide(policy(), "npm", "left-pad", ["1.0.0", "9.9.9"])
    finally:
        osv.stop()

    assert first.status == "ok"
    assert second.status == "degraded"
    assert [(v.version, v.blocked) for v in second.verdicts] == [("1.0.0", True), ("9.9.9", False)]


def test_osv_querybatch_endpoint_returns_verdicts():
    osv = MockOsv()
    osv.malicious["1.0.0"] = ["MAL-2026-1"]
    osv.start()
    parsed_store = ParsedPolicyStore()
    parsed_store.set(policy())
    httpd = PolicySyncHTTPServer(
        ("127.0.0.1", 0),
        TEST_SECRET,
        lambda: None,
        SyncState(),
        PolicyStore(),
        PolicyStore(),
        parsed_store,
        OsvClient(api_url=osv.url),
    )
    thread = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{httpd.server_address[1]}/osv/querybatch",
            data=json.dumps({"ecosystem": "pypi", "name": "six", "versions": ["1.0.0"]}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read())
    finally:
        httpd.shutdown()
        httpd.server_close()
        osv.stop()

    assert body == {
        "status": "ok",
        "results": [{"version": "1.0.0", "blocked": True, "ids": ["MAL-2026-1"]}],
    }
