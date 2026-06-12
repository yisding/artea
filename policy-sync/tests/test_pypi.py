import io
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from policy_sync.config import Config
from policy_sync.pypi import PypiProxy, parse_pypi_policy
from policy_sync.store import PolicyStore
from tests.conftest import TEST_DEVPI_PASSWORD, TEST_REPO, TEST_SECRET, TEST_TOKEN


class FakeHandler:
    def __init__(self, path="/pypi/simple/six/"):
        self.path = path
        self.headers = {
            "Host": "registry.test",
            "X-Forwarded-Proto": "http",
            "X-Forwarded-Host": "registry.test",
        }
        self.status = None
        self.response_headers = []
        self.wfile = io.BytesIO()

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        self.response_headers.append((key, value))

    def end_headers(self):
        pass

    def _respond(self, code, payload):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.wfile.write(json.dumps(payload).encode())


class RouteServer:
    def __init__(self, routes):
        self.routes = routes
        routes_ref = self.routes

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                status, headers, body = routes_ref.get(self.path, (404, {}, b"not found"))
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_HEAD(self):
                status, headers, body = routes_ref.get(self.path, (404, {}, b"not found"))
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=lambda: self.httpd.serve_forever(poll_interval=0.01), daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def start(self):
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def cfg(devpi_url, pypi_json_url):
    return Config(
        gitea_url="http://gitea:3000",
        sync_token=TEST_TOKEN,
        webhook_secret=TEST_SECRET,
        policy_repo=TEST_REPO,
        policy_file_path="",
        pypi_policy_file_path="",
        devpi_url=devpi_url,
        devpi_root_password=TEST_DEVPI_PASSWORD,
        pypi_json_url=pypi_json_url,
        pypi_metadata_cache_seconds=300,
        poll_interval=300,
    )


def iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def make_proxy():
    old_file = "six-1.0.0-py3-none-any.whl"
    new_file = "six-2.0.0-py3-none-any.whl"
    simple = f"""<!DOCTYPE html>
<html><body>
<a href="http://registry.test/root/pypi/+f/old/{old_file}#sha256=old">old</a>
<a href="http://registry.test/root/pypi/+f/new/{new_file}#sha256=new">new</a>
</body></html>
""".encode()
    devpi = RouteServer({
        "/root/constrained/+simple/six/": (200, {"Content-Type": "text/html"}, simple),
        f"/root/pypi/+f/old/{old_file}": (200, {"Content-Type": "application/octet-stream"}, b"old wheel"),
        f"/root/pypi/+f/new/{new_file}": (200, {"Content-Type": "application/octet-stream"}, b"new wheel"),
    })
    pypi = RouteServer({
        "/pypi/six/json": (
            200,
            {"Content-Type": "application/json"},
            json.dumps({
                "releases": {
                    "1.0.0": [{"filename": old_file, "upload_time_iso_8601": iso(10)}],
                    "2.0.0": [{"filename": new_file, "upload_time_iso_8601": iso(1)}],
                }
            }).encode(),
        )
    })
    devpi.start()
    pypi.start()
    store = PolicyStore()
    store.set(b"# artea: min-upstream-age=3d\n")
    proxy = PypiProxy(cfg(devpi.url, f"{pypi.url}/pypi"), store)
    return proxy, devpi, pypi


def test_parse_pypi_age_directive():
    policy = parse_pypi_policy("# artea: min-upstream-age=3d\nurllib3<2\n")
    assert policy.min_age_seconds == 3 * 24 * 60 * 60


def test_simple_page_hides_too_new_files_and_rewrites_guarded_urls():
    proxy, devpi, pypi = make_proxy()
    try:
        handler = FakeHandler()
        proxy.serve_simple(handler, "six")
        body = handler.wfile.getvalue().decode()

        assert handler.status == 200
        assert "six-1.0.0" in body
        assert "six-2.0.0" not in body
        assert "http://registry.test/pypi/files/six/root/pypi/+f/old/six-1.0.0-py3-none-any.whl#sha256=old" in body
    finally:
        devpi.stop()
        pypi.stop()


def test_guarded_file_allows_old_and_blocks_young_direct_urls():
    proxy, devpi, pypi = make_proxy()
    try:
        # First serve the simple page so direct /root/ links have a project cache.
        proxy.serve_simple(FakeHandler(), "six")

        old_handler = FakeHandler("/pypi/files/six/root/pypi/+f/old/six-1.0.0-py3-none-any.whl")
        proxy.serve(old_handler)
        assert old_handler.status == 200
        assert old_handler.wfile.getvalue() == b"old wheel"

        new_handler = FakeHandler("/pypi/files/six/root/pypi/+f/new/six-2.0.0-py3-none-any.whl")
        proxy.serve(new_handler)
        assert new_handler.status == 403
        assert b"minimum upstream age" in new_handler.wfile.getvalue()

        direct_handler = FakeHandler("/root/pypi/+f/new/six-2.0.0-py3-none-any.whl")
        proxy.serve(direct_handler)
        assert direct_handler.status == 403
    finally:
        devpi.stop()
        pypi.stop()


def test_missing_pypi_policy_fails_closed(mock_devpi):
    store = PolicyStore()
    proxy = PypiProxy(cfg(mock_devpi.url, "http://pypi.example.test/pypi"), store)
    handler = FakeHandler()

    proxy.serve_simple(handler, "six")

    assert handler.status == 503
    assert b"pypi policy unavailable" in handler.wfile.getvalue()
