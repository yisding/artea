import json
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pytest

from policy_sync.osv import OsvClient, OsvVerdict
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
        self.paths: list[str] = []
        super().__init__()

    def _build_handler(self):
        mock = self

        class Handler(_StubHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                payload = json.loads(body)
                mock.paths.append(self.path)
                mock.requests.append(payload)  # MockOsv captures parsed payload
                if mock.fail:
                    self.send_error(500)
                    return
                if self.path == "/v1/query":
                    vulns = []
                    for version, ids in mock.malicious.items():
                        vulns.extend(
                            {
                                "id": mid,
                                "modified": "2026-01-01T00:00:00Z",
                                "affected": [{"versions": [version]}],
                            }
                            for mid in ids
                        )
                    if mock.vulnerable:
                        vulns.append({"id": "GHSA-xxxx-yyyy-zzzz", "modified": "2026-01-01T00:00:00Z"})
                    reply(self, 200, json.dumps({"vulns": vulns}).encode())
                    return
                if self.path != "/v1/querybatch":
                    self.send_error(404)
                    return
                queries = payload.get("queries", [])
                results = []
                for query in queries:
                    version = query.get("version")
                    if version is None:
                        vulns = []
                        for affected_version, ids in mock.malicious.items():
                            vulns.extend(
                                {
                                    "id": mid,
                                    "modified": "2026-01-01T00:00:00Z",
                                    "affected": [{"versions": [affected_version]}],
                                }
                                for mid in ids
                            )
                        if mock.vulnerable:
                            vulns.append({"id": "GHSA-xxxx-yyyy-zzzz", "modified": "2026-01-01T00:00:00Z"})
                        results.append({"vulns": vulns} if vulns else {})
                        continue
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
    assert osv.paths == ["/v1/querybatch"]
    assert osv.requests[0] == {"queries": [{"package": {"name": "left-pad", "ecosystem": "npm"}}]}


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
    assert osv.paths == ["/v1/querybatch"]
    assert osv.requests[0] == {"queries": [{"package": {"name": "six", "ecosystem": "PyPI"}}]}


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
    assert osv.paths == ["/v1/querybatch"]
    assert osv.requests[0] == {"queries": [{"package": {"name": "six", "ecosystem": "PyPI"}}]}


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
    # the OSV request must use OSV's "PyPI" ecosystem casing
    assert osv.paths == ["/v1/querybatch"]
    assert osv.requests[0]["queries"][0]["package"]["ecosystem"] == "PyPI"


def test_package_level_osv_query_allows_when_no_malicious_records(monkeypatch):
    calls = []
    client = OsvClient(api_url="http://osv.example.test", batch_size=1)

    def fake_post_json(path, payload):
        calls.append((path, payload))
        assert path == "/v1/querybatch"
        return {"results": [{"vulns": [{"id": "GHSA-xxxx-yyyy-zzzz"}]}]}

    monkeypatch.setattr(client, "_post_json", fake_post_json)

    result = client.decide(policy(), "npm", "left-pad", ["1.0.0", "2.0.0"])

    assert result.status == "ok"
    assert [(v.version, v.blocked) for v in result.verdicts] == [("1.0.0", False), ("2.0.0", False)]
    assert calls == [("/v1/querybatch", {"queries": [{"package": {"name": "left-pad", "ecosystem": "npm"}}]})]


def test_package_level_osv_query_blocks_exact_malicious_versions(monkeypatch):
    calls = []
    client = OsvClient(api_url="http://osv.example.test", batch_size=1)

    def fake_post_json(path, payload):
        calls.append((path, payload))
        assert path == "/v1/querybatch"
        return {
            "results": [
                {
                    "vulns": [
                        {
                            "id": "MAL-2026-1",
                            "affected": [{"versions": ["2.0.0", "9.9.9"]}],
                        }
                    ]
                }
            ],
        }

    monkeypatch.setattr(client, "_post_json", fake_post_json)

    result = client.decide(policy(), "npm", "left-pad", ["1.0.0", "2.0.0"])

    assert result.status == "ok"
    assert [(v.version, v.blocked, v.ids) for v in result.verdicts] == [
        ("1.0.0", False, ()),
        ("2.0.0", True, ("MAL-2026-1",)),
    ]
    assert calls == [("/v1/querybatch", {"queries": [{"package": {"name": "left-pad", "ecosystem": "npm"}}]})]


def test_package_level_osv_query_blocks_semver_range_malicious_versions(monkeypatch):
    calls = []
    client = OsvClient(api_url="http://osv.example.test", batch_size=1)

    def fake_post_json(path, payload):
        calls.append((path, payload))
        assert path == "/v1/querybatch"
        return {
            "results": [
                {
                    "vulns": [
                        {
                            "id": "MAL-2026-1",
                            "affected": [
                                {
                                    "ranges": [
                                        {
                                            "type": "SEMVER",
                                            "events": [
                                                {"introduced": "2.0.0"},
                                                {"fixed": "2.1.0"},
                                            ],
                                        }
                                    ]
                                }
                            ],
                        }
                    ]
                }
            ],
        }

    monkeypatch.setattr(client, "_post_json", fake_post_json)

    result = client.decide(policy(), "npm", "left-pad", ["1.0.0", "2.0.0", "2.0.5", "2.1.0"])

    assert [(v.version, v.blocked, v.ids) for v in result.verdicts] == [
        ("1.0.0", False, ()),
        ("2.0.0", True, ("MAL-2026-1",)),
        ("2.0.5", True, ("MAL-2026-1",)),
        ("2.1.0", False, ()),
    ]
    assert calls == [("/v1/querybatch", {"queries": [{"package": {"name": "left-pad", "ecosystem": "npm"}}]})]


def test_package_level_osv_query_falls_back_when_malicious_range_lacks_exact_versions(monkeypatch):
    calls = []
    client = OsvClient(api_url="http://osv.example.test", batch_size=1)

    def fake_post_json(path, payload):
        calls.append((path, payload))
        if len(calls) == 1:
            assert path == "/v1/querybatch"
            return {
                "results": [
                    {
                        "vulns": [
                            {
                                "id": "MAL-2026-1",
                                "affected": [{"ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "2.0.0"}]}]}],
                            }
                        ]
                    }
                ],
            }
        assert path == "/v1/querybatch"
        versions = [query["version"] for query in payload["queries"]]
        return {
            "results": [
                {
                    "vulns": [{"id": "MAL-2026-1", "modified": "2026-01-01T00:00:00Z"}]
                    if version == "2.0.0"
                    else []
                }
                for version in versions
            ]
        }

    monkeypatch.setattr(client, "_post_json", fake_post_json)

    result = client.decide(policy(), "npm", "left-pad", ["1.0.0", "2.0.0"])

    assert result.status == "ok"
    assert [(v.version, v.blocked) for v in result.verdicts] == [("1.0.0", False), ("2.0.0", True)]
    assert [path for path, _payload in calls] == ["/v1/querybatch", "/v1/querybatch", "/v1/querybatch"]


def test_small_candidate_sets_still_use_package_fast_path(monkeypatch):
    calls = []
    client = OsvClient(api_url="http://osv.example.test")

    def fake_post_json(path, payload):
        calls.append((path, payload))
        assert path == "/v1/querybatch"
        assert all("version" not in query for query in payload["queries"])
        return {"results": [{"vulns": [{"id": "GHSA-xxxx-yyyy-zzzz"}]}]}

    monkeypatch.setattr(client, "_post_json", fake_post_json)

    result = client.decide(policy(), "npm", "left-pad", ["1.0.0", "2.0.0"])

    assert [(v.version, v.blocked) for v in result.verdicts] == [("1.0.0", False), ("2.0.0", False)]
    assert calls == [
        (
            "/v1/querybatch",
            {
                "queries": [
                    {"package": {"name": "left-pad", "ecosystem": "npm"}},
                ]
            },
        )
    ]


def test_package_summary_returns_only_exact_malicious_versions(monkeypatch):
    calls = []
    client = OsvClient(api_url="http://osv.example.test")

    def fake_post_json(path, payload):
        calls.append((path, payload))
        assert path == "/v1/querybatch"
        return {
            "results": [
                {
                    "vulns": [
                        {
                            "id": "MAL-2026-1",
                            "affected": [{"versions": ["2.0.0", "9.9.9"]}],
                        },
                        {"id": "GHSA-xxxx-yyyy-zzzz"},
                    ]
                }
            ],
        }

    monkeypatch.setattr(client, "_post_json", fake_post_json)

    result = client.summarize_package(policy(), "npm", "left-pad")

    assert result.status == "ok"
    assert [(v.version, v.blocked, v.ids) for v in result.verdicts] == [
        ("2.0.0", True, ("MAL-2026-1",)),
        ("9.9.9", True, ("MAL-2026-1",)),
    ]
    assert calls == [("/v1/querybatch", {"queries": [{"package": {"name": "left-pad", "ecosystem": "npm"}}]})]


def test_package_summary_honors_curated_allow_overrides(monkeypatch):
    client = OsvClient(api_url="http://osv.example.test")

    def fake_post_json(_path, _payload):
        return {
            "results": [
                {
                    "vulns": [
                        {
                            "id": "MAL-2026-1",
                            "affected": [{"versions": ["2.0.0", "9.9.9"]}],
                        }
                    ]
                }
            ],
        }

    monkeypatch.setattr(client, "_post_json", fake_post_json)

    result = client.summarize_package(policy(
        '[[rules]]\n'
        'ecosystem = "npm"\n'
        'name = "left-pad"\n'
        'versions = "2.0.0"\n'
        'action = "allow"\n'
    ), "npm", "left-pad")

    assert [(v.version, v.blocked, v.ids) for v in result.verdicts] == [
        ("9.9.9", True, ("MAL-2026-1",)),
    ]


def test_package_summary_warms_later_versioned_decision(monkeypatch):
    calls = []
    client = OsvClient(api_url="http://osv.example.test")

    def fake_post_json(path, payload):
        calls.append((path, payload))
        assert path == "/v1/querybatch"
        return {"results": [{"vulns": []}]}

    monkeypatch.setattr(client, "_post_json", fake_post_json)

    summary = client.summarize_package(policy(), "npm", "left-pad")
    result = client.decide(policy(), "npm", "left-pad", ["1.0.0", "2.0.0"])

    assert summary.status == "ok"
    assert [(v.version, v.blocked) for v in result.verdicts] == [("1.0.0", False), ("2.0.0", False)]
    assert calls == [("/v1/querybatch", {"queries": [{"package": {"name": "left-pad", "ecosystem": "npm"}}]})]


def test_package_summary_needs_versions_when_malicious_range_lacks_exact_versions(monkeypatch):
    client = OsvClient(api_url="http://osv.example.test")

    def fake_post_json(_path, _payload):
        return {
            "results": [
                {
                    "vulns": [
                        {
                            "id": "MAL-2026-1",
                            "affected": [{"ranges": [{"type": "SEMVER", "events": [{"introduced": "2.0.0"}]}]}],
                        }
                    ]
                }
            ],
        }

    monkeypatch.setattr(client, "_post_json", fake_post_json)

    result = client.summarize_package(policy(), "npm", "left-pad")

    assert result.status == "needs_versions"
    assert result.verdicts == ()


def test_osv_chunks_are_dispatched_concurrently(monkeypatch):
    client = OsvClient(api_url="http://osv.example.test", batch_size=1, max_concurrency=4)

    def fake_query_chunk(ecosystem, name, versions):
        time.sleep(0.05)
        return {
            version: OsvVerdict(version=version, blocked=False)
            for version in versions
        }

    monkeypatch.setattr(client, "_query_chunk", fake_query_chunk)
    started = time.perf_counter()
    result = client._query_osv_versions("npm", "left-pad", ["1.0.0", "2.0.0", "3.0.0", "4.0.0"])

    assert time.perf_counter() - started < 0.15
    assert set(result) == {"1.0.0", "2.0.0", "3.0.0", "4.0.0"}


def test_osv_external_requests_are_bounded_across_decisions(monkeypatch):
    client = OsvClient(api_url="http://osv.example.test", batch_size=2, max_concurrency=2)
    active = 0
    max_active = 0
    calls = []
    lock = threading.Lock()

    def fake_post_json_unbounded(path, payload):
        nonlocal active, max_active
        assert path == "/v1/querybatch"
        with lock:
            calls.append(payload)
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return {"results": [{} for _query in payload["queries"]]}

    monkeypatch.setattr(client, "_post_json_unbounded", fake_post_json_unbounded)

    with ThreadPoolExecutor(max_workers=6) as executor:
        list(executor.map(
            lambda i: client._query_osv("npm", f"pkg-{i}", ["1.0.0", "2.0.0", "3.0.0"]),
            range(6),
        ))

    assert max_active <= 2
    assert sum(len(call["queries"]) for call in calls) == 6
    assert any(len(call["queries"]) > 1 for call in calls)


def test_decide_rejects_unknown_ecosystem():
    from policy_sync.policy_model import PolicyError

    client = OsvClient(api_url="http://127.0.0.1:1")  # never contacted
    with pytest.raises(PolicyError):
        client.decide(policy(), "cargo", "serde", ["1.0.0"])


def test_osv_outage_uses_cached_package_page_and_malicious_verdict():
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
    assert second.status == "ok"
    assert [(v.version, v.blocked) for v in second.verdicts] == [("1.0.0", True), ("9.9.9", False)]


def test_osv_verdict_cache_persists_and_reloads(tmp_path, monkeypatch):
    cache_file = tmp_path / "osv-cache.json"
    client = OsvClient(api_url="http://osv.example.test", cache_file_path=str(cache_file), now=lambda: 1000.0)

    def fake_post_json(path, payload):
        assert path == "/v1/querybatch"
        assert all("version" not in query for query in payload["queries"])
        return {
            "results": [
                {
                    "vulns": [
                        {
                            "id": "MAL-2026-1",
                            "affected": [{"versions": ["1.0.0"]}],
                        }
                    ]
                }
            ],
        }

    monkeypatch.setattr(client, "_post_json", fake_post_json)
    first = client.decide(policy(), "npm", "left-pad", ["1.0.0", "2.0.0"])
    assert [(v.version, v.blocked) for v in first.verdicts] == [("1.0.0", True), ("2.0.0", False)]

    reloaded = OsvClient(api_url="http://osv.example.test", cache_file_path=str(cache_file), now=lambda: 1001.0)
    monkeypatch.setattr(reloaded, "_post_json", lambda _path, _payload: pytest.fail("cache miss"))
    second = reloaded.decide(policy(), "npm", "left-pad", ["1.0.0", "2.0.0"])

    assert [(v.version, v.blocked) for v in second.verdicts] == [("1.0.0", True), ("2.0.0", False)]


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


def test_osv_querybatch_endpoint_supports_package_summary_without_versions():
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
            data=json.dumps({"ecosystem": "npm", "name": "left-pad", "package_summary": True}).encode(),
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


def test_osv_querybatch_endpoint_uses_parsed_policy_fallback(tmp_path):
    osv = MockOsv()
    osv.malicious["1.0.0"] = ["MAL-2026-1"]
    osv.start()
    fallback = tmp_path / "policy.toml"
    fallback.write_bytes(b"schema = 1\n[osv]\nmalicious_packages = true\n")
    parsed_store = ParsedPolicyStore(fallback_path=str(fallback))
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
            data=json.dumps({"ecosystem": "npm", "name": "left-pad", "versions": ["1.0.0"]}).encode(),
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
