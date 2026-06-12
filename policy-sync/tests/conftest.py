import base64
import json
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from policy_sync.config import Config

TEST_TOKEN = "test-pat-token"
TEST_SECRET = "test-webhook-secret"
TEST_REPO = "artea/registry-policy"
TEST_DEVPI_PASSWORD = "devpi-pass"


class MockGitea:
    """Minimal in-process Gitea that serves the raw-content API."""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.requests: list[dict] = []
        self.fail_remaining = 0  # respond 500 to the next N requests
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                mock.requests.append({
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                })
                if mock.fail_remaining > 0:
                    mock.fail_remaining -= 1
                    self.send_error(500)
                    return
                if self.headers.get("Authorization") != f"token {TEST_TOKEN}":
                    self.send_error(401)
                    return
                prefix = f"/api/v1/repos/{TEST_REPO}/raw/"
                if not self.path.startswith(prefix):
                    self.send_error(404)
                    return
                name = self.path[len(prefix):]
                if name not in mock.files:
                    self.send_error(404)
                    return
                body = mock.files[name]
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        # short poll so shutdown() in teardown returns quickly
        self.thread = threading.Thread(target=lambda: self.httpd.serve_forever(poll_interval=0.01), daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def start(self):
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def mock_gitea():
    server = MockGitea()
    server.start()
    yield server
    server.stop()


class MockDevpi:
    """Minimal in-process devpi serving the root/constrained index JSON API."""

    def __init__(self):
        # shape of the Artea constrained index config (subset)
        self.config: dict = {"type": "constrained", "bases": ["root/pypi"], "constraints": [], "min_upstream_age": "P0D"}
        self.requests: list[dict] = []
        self.fail_remaining = 0  # respond 500 to the next N requests
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def _record(self, body=None):
                mock.requests.append({
                    "method": self.command,
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "body": body,
                })

            def _json(self, code, payload):
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._record()
                if mock.fail_remaining > 0:
                    mock.fail_remaining -= 1
                    self.send_error(500)
                    return
                if self.path != "/root/constrained":
                    self.send_error(404)
                    return
                self._json(200, {"type": "indexconfig", "result": dict(mock.config)})

            def do_PATCH(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self._record(body)
                if mock.fail_remaining > 0:
                    mock.fail_remaining -= 1
                    self.send_error(500)
                    return
                expected = "Basic " + base64.b64encode(f"root:{TEST_DEVPI_PASSWORD}".encode()).decode()
                if self.headers.get("Authorization") != expected:
                    self.send_error(403)
                    return
                if self.path != "/root/constrained":
                    self.send_error(404)
                    return
                mock.config = json.loads(body)
                self._json(200, {"type": "indexconfig", "result": dict(mock.config)})

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=lambda: self.httpd.serve_forever(poll_interval=0.01), daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    @property
    def patches(self) -> list[dict]:
        return [r for r in self.requests if r["method"] == "PATCH"]

    def start(self):
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def mock_devpi():
    server = MockDevpi()
    server.start()
    yield server
    server.stop()


def make_config(mock_gitea, mock_devpi, policy_file_path: str) -> Config:
    return Config(
        gitea_url=mock_gitea.url,
        sync_token=TEST_TOKEN,
        webhook_secret=TEST_SECRET,
        policy_repo=TEST_REPO,
        policy_file_path=policy_file_path,
        upstream_policy_file_path=str(Path(policy_file_path).with_name("upstream-policy.yaml")) if policy_file_path else "",
        pypi_policy_file_path=str(Path(policy_file_path).with_name("pypi-constraints.txt")) if policy_file_path else "",
        devpi_url=mock_devpi.url,
        devpi_root_password=TEST_DEVPI_PASSWORD,
        poll_interval=300,
    )


@pytest.fixture
def cfg(mock_gitea, mock_devpi, tmp_path):
    return make_config(mock_gitea, mock_devpi, str(tmp_path / "npm-rules.yaml"))


@pytest.fixture
def cfg_http_only(mock_gitea, mock_devpi):
    """POLICY_FILE_PATH="" — the K8s shape: no file write at all."""
    return make_config(mock_gitea, mock_devpi, "")
