import hashlib
import hmac
import json
import threading
import urllib.error
import urllib.request

import pytest

from policy_sync.server import SyncState, make_http_server
from policy_sync.store import PolicyStore, etag_for
from tests.conftest import TEST_SECRET

PAYLOAD = json.dumps({"ref": "refs/heads/main", "repository": {"full_name": "artea/registry-policy"}}).encode()

POLICY = b"blocked:\n  packages:\n    - left-pad\n"
UPSTREAM = b"upstream:\n  min_age: P3D\n"


def sign(body: bytes, secret: str = TEST_SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def service(tmp_path, cfg):
    triggers = []
    state = SyncState()
    store = PolicyStore(fallback_path=str(tmp_path / "npm-rules.yaml"))
    upstream_store = PolicyStore(fallback_path=str(tmp_path / "upstream-policy.yaml"))
    httpd = make_http_server("127.0.0.1", 0, TEST_SECRET, lambda: triggers.append(1), state, store, upstream_store)
    thread = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield url, triggers, state, store, upstream_store
    httpd.shutdown()
    httpd.server_close()


def get_policy(url: str, if_none_match: str | None = None) -> tuple[int, dict, bytes]:
    """GET /policy/npm-rules.yaml -> (status, headers, body)."""
    headers = {"If-None-Match": if_none_match} if if_none_match else {}
    req = urllib.request.Request(url + "/policy/npm-rules.yaml", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def get_upstream_policy(url: str) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url + "/policy/upstream-policy.yaml")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def head(url: str, path: str) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url + path, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def post(url: str, body: bytes, headers: dict, path: str = "/hooks/policy") -> tuple[int, dict]:
    req = urllib.request.Request(url + path, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_healthz_ok(service):
    url, _, state, _, _ = service
    state.record(True)
    with urllib.request.urlopen(url + "/healthz", timeout=5) as resp:
        assert resp.status == 200
        body = json.loads(resp.read())
    assert body["status"] == "ok"
    assert body["last_sync_ok"] is True
    assert body["last_sync_at"] is not None


def test_valid_push_webhook_triggers_sync(service):
    url, triggers, _, _, _ = service
    status, body = post(url, PAYLOAD, {"X-Gitea-Signature": sign(PAYLOAD), "X-Gitea-Event": "push"})
    assert status == 202
    assert body == {"status": "sync scheduled"}
    assert triggers == [1]


def test_invalid_signature_rejected(service):
    url, triggers, _, _, _ = service
    status, _ = post(url, PAYLOAD, {"X-Gitea-Signature": sign(PAYLOAD, "wrong-secret"), "X-Gitea-Event": "push"})
    assert status == 403
    assert triggers == []


def test_missing_signature_rejected(service):
    url, triggers, _, _, _ = service
    status, _ = post(url, PAYLOAD, {"X-Gitea-Event": "push"})
    assert status == 403
    assert triggers == []


def test_tampered_body_rejected(service):
    url, triggers, _, _, _ = service
    status, _ = post(url, PAYLOAD + b"x", {"X-Gitea-Signature": sign(PAYLOAD), "X-Gitea-Event": "push"})
    assert status == 403
    assert triggers == []


def test_non_push_event_ignored(service):
    url, triggers, _, _, _ = service
    status, body = post(url, PAYLOAD, {"X-Gitea-Signature": sign(PAYLOAD), "X-Gitea-Event": "issues"})
    assert status == 200
    assert body["status"] == "ignored"
    assert triggers == []


def test_unknown_paths_404(service):
    url, _, _, _, _ = service
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(url + "/nope", timeout=5)
    assert exc.value.code == 404
    status, _ = post(url, PAYLOAD, {"X-Gitea-Signature": sign(PAYLOAD)}, path="/nope")
    assert status == 404


def test_head_error_responses_have_no_body(service):
    url, _, _, _, _ = service
    status, headers, body = head(url, "/nope")
    assert status == 404
    assert headers["Content-Type"] == "application/json"
    assert body == b""


def test_policy_404_before_first_sync(service):
    url, _, _, _, _ = service  # store empty, fallback file does not exist
    status, _, body = get_policy(url)
    assert status == 404
    assert b"no npm policy has been synced yet" in body


def test_policy_200_with_strong_etag(service):
    url, _, _, store, _ = service
    store.set(POLICY)
    status, headers, body = get_policy(url)
    assert status == 200
    assert body == POLICY
    assert headers["ETag"] == etag_for(POLICY)  # strong, content-derived
    assert headers["Content-Type"] == "application/yaml"
    assert int(headers["Content-Length"]) == len(POLICY)


def test_upstream_policy_endpoint(service):
    url, _, _, _, upstream_store = service
    upstream_store.set(UPSTREAM)
    status, headers, body = get_upstream_policy(url)
    assert status == 200
    assert body == UPSTREAM
    assert headers["ETag"] == etag_for(UPSTREAM)


def test_policy_304_on_matching_etag(service):
    url, _, _, store, _ = service
    store.set(POLICY)
    etag = etag_for(POLICY)
    status, headers, body = get_policy(url, if_none_match=etag)
    assert status == 304
    assert body == b""
    assert headers["ETag"] == etag
    # weak-form and list-form If-None-Match also match (RFC 9110 weak comparison)
    assert get_policy(url, if_none_match=f'W/{etag}, "other"')[0] == 304
    assert get_policy(url, if_none_match="*")[0] == 304


def test_policy_etag_changes_with_content(service):
    url, _, _, store, _ = service
    store.set(POLICY)
    old_etag = etag_for(POLICY)

    store.set(b"blocked: {}\n")
    status, headers, body = get_policy(url, if_none_match=old_etag)
    assert status == 200  # stale etag no longer matches
    assert body == b"blocked: {}\n"
    assert headers["ETag"] != old_etag


def test_policy_falls_back_to_file_when_memory_empty(service, tmp_path):
    # compose restart: the volume still holds the last synced file
    url, _, _, store, _ = service
    (tmp_path / "npm-rules.yaml").write_bytes(POLICY)
    status, headers, body = get_policy(url)
    assert status == 200
    assert body == POLICY
    assert headers["ETag"] == etag_for(POLICY)  # same content, same etag as memory mode


def test_policy_http_only_store_has_no_fallback(service):
    # HTTP-only mode: PolicyStore("") never touches the filesystem
    assert PolicyStore("").get() is None
    store = PolicyStore("")
    store.set(POLICY)
    assert store.get() == (POLICY, etag_for(POLICY))
