import hashlib
import hmac
import json
import threading
import urllib.error
import urllib.request

import pytest

from policy_sync.server import SyncState, make_http_server
from tests.conftest import TEST_SECRET

PAYLOAD = json.dumps({"ref": "refs/heads/main", "repository": {"full_name": "artea/registry-policy"}}).encode()


def sign(body: bytes, secret: str = TEST_SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def service():
    triggers = []
    state = SyncState()
    httpd = make_http_server("127.0.0.1", 0, TEST_SECRET, lambda: triggers.append(1), state)
    thread = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield url, triggers, state
    httpd.shutdown()
    httpd.server_close()


def post(url: str, body: bytes, headers: dict, path: str = "/hooks/policy") -> tuple[int, dict]:
    req = urllib.request.Request(url + path, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_healthz_ok(service):
    url, _, state = service
    state.record(True)
    with urllib.request.urlopen(url + "/healthz", timeout=5) as resp:
        assert resp.status == 200
        body = json.loads(resp.read())
    assert body["status"] == "ok"
    assert body["last_sync_ok"] is True
    assert body["last_sync_at"] is not None


def test_valid_push_webhook_triggers_sync(service):
    url, triggers, _ = service
    status, body = post(url, PAYLOAD, {"X-Gitea-Signature": sign(PAYLOAD), "X-Gitea-Event": "push"})
    assert status == 202
    assert body == {"status": "sync scheduled"}
    assert triggers == [1]


def test_invalid_signature_rejected(service):
    url, triggers, _ = service
    status, _ = post(url, PAYLOAD, {"X-Gitea-Signature": sign(PAYLOAD, "wrong-secret"), "X-Gitea-Event": "push"})
    assert status == 403
    assert triggers == []


def test_missing_signature_rejected(service):
    url, triggers, _ = service
    status, _ = post(url, PAYLOAD, {"X-Gitea-Event": "push"})
    assert status == 403
    assert triggers == []


def test_tampered_body_rejected(service):
    url, triggers, _ = service
    status, _ = post(url, PAYLOAD + b"x", {"X-Gitea-Signature": sign(PAYLOAD), "X-Gitea-Event": "push"})
    assert status == 403
    assert triggers == []


def test_non_push_event_ignored(service):
    url, triggers, _ = service
    status, body = post(url, PAYLOAD, {"X-Gitea-Signature": sign(PAYLOAD), "X-Gitea-Event": "issues"})
    assert status == 200
    assert body["status"] == "ignored"
    assert triggers == []


def test_unknown_paths_404(service):
    url, _, _ = service
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(url + "/nope", timeout=5)
    assert exc.value.code == 404
    status, _ = post(url, PAYLOAD, {"X-Gitea-Signature": sign(PAYLOAD)}, path="/nope")
    assert status == 404
