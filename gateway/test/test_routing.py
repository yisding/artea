# Copyright 2026 The Artea Authors. All rights reserved.
#
# Functional test for gateway/nginx.conf: runs a real nginx (must be on PATH)
# on a loopback port against stdlib stub upstreams, then asserts the routing
# contract from docs/ARCHITECTURE.md. Stdlib-only; works with unittest or pytest:
#
#   python3 gateway/test/test_routing.py
#
# The only edits made to the config under test are filesystem paths, the listen
# port, and the three upstream host:port values (hostnames only resolve inside
# docker compose). All routing/rewrite/auth logic is exercised unmodified.

import http.client
import http.server
import pathlib
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest

CONF = pathlib.Path(__file__).resolve().parent.parent / "nginx.conf"

GOOD_AUTH = "Basic dXNlcjpnb29kLXBhdA=="  # user:good-pat
BAD_AUTH = "Basic dXNlcjpyZXZva2Vk"  # user:revoked


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Upstream(http.server.ThreadingHTTPServer):
    """Records every request path; per-instance handler logic via `tag`."""

    def __init__(self, tag):
        self.tag = tag
        self.requests = []  # (path, authorization) tuples
        super().__init__(("127.0.0.1", 0), UpstreamHandler)

    def handle_error(self, request, client_address):
        pass  # nginx resets idle keepalive conns at shutdown; not an error


class UpstreamHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # gateway proxies with HTTP/1.1

    def log_message(self, *args):
        pass

    def _reply(self, code, body, headers=()):
        data = body.encode()
        self.send_response(code)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        server = self.server
        auth = self.headers.get("Authorization")
        server.requests.append((self.path, auth))
        if server.tag == "gitea":
            if self.path == "/api/v1/user":
                if auth == GOOD_AUTH:
                    self._reply(200, '{"login":"user"}')
                else:
                    self._reply(401, "unauthorized")
                return
            # private package exists; "six" does not (mirrors Gitea's 404)
            if self.path.startswith("/api/packages/artea/pypi/simple/private-pkg"):
                self._reply(200, "gitea-simple private-pkg")
                return
            if self.path.startswith("/api/packages/artea/pypi/simple/"):
                self._reply(404, "package does not exist")
                return
        self._reply(200, f"{server.tag} {self.path}")

    do_POST = do_GET
    do_PUT = do_GET


class GatewayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which("nginx") is None:
            raise unittest.SkipTest("nginx binary not on PATH")
        cls.tmp = tempfile.TemporaryDirectory(prefix="artea-gw-test.")
        tmp = pathlib.Path(cls.tmp.name)
        (tmp / "logs").mkdir()
        (tmp / "cache").mkdir()

        cls.upstreams = {t: Upstream(t) for t in ("gitea", "verdaccio", "devpi")}
        for up in cls.upstreams.values():
            threading.Thread(target=up.serve_forever, daemon=True).start()

        cls.port = free_port()
        conf = CONF.read_text()
        subs = {
            "listen 80;": f"listen 127.0.0.1:{cls.port};",
            "gitea:3000": "127.0.0.1:%d" % cls.upstreams["gitea"].server_port,
            "verdaccio:4873": "127.0.0.1:%d" % cls.upstreams["verdaccio"].server_port,
            "devpi:3141": "127.0.0.1:%d" % cls.upstreams["devpi"].server_port,
            "/var/log/nginx": str(tmp / "logs"),
            "/var/cache/nginx": str(tmp / "cache"),
            "/var/run/nginx.pid": str(tmp / "nginx.pid"),
        }
        for old, new in subs.items():
            assert old in conf, f"expected literal not found in nginx.conf: {old}"
            conf = conf.replace(old, new)
        conf_path = tmp / "nginx.conf"
        conf_path.write_text(conf)

        cls.nginx = subprocess.Popen(
            ["nginx", "-p", str(tmp), "-c", str(conf_path), "-g", "daemon off;"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 5
        while True:
            try:
                status, _, _ = cls._raw("GET", "/-/artea-gateway/health")
                if status == 200:
                    break
            except OSError:
                pass
            if time.monotonic() > deadline:
                err = b""
                if cls.nginx.poll() is not None:
                    err = cls.nginx.stderr.read()
                raise RuntimeError(f"nginx did not come up: {err.decode()}")
            time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.nginx.terminate()
        cls.nginx.wait(timeout=5)
        for up in cls.upstreams.values():
            up.shutdown()
        cls.tmp.cleanup()

    @classmethod
    def _raw(cls, method, path, auth=None):
        """http.client keeps the path byte-exact (no normalization)."""
        conn = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=5)
        headers = {"Authorization": auth} if auth else {}
        conn.request(method, path, headers=headers)
        resp = conn.getresponse()
        body = resp.read().decode()
        headers = dict(resp.getheaders())
        conn.close()
        return resp.status, body, headers

    def seen(self, tag):
        return [p for p, _ in self.upstreams[tag].requests]

    # ---- routing table ----

    def test_health(self):
        status, body, _ = self._raw("GET", "/-/artea-gateway/health")
        self.assertEqual((status, body), (200, "ok\n"))

    def test_catchall_to_gitea_preserves_raw_uri(self):
        # npm scoped paths use %2f; the gateway must not decode or re-encode them
        path = "/api/packages/artea/npm/@artea%2fhello"
        status, body, _ = self._raw("GET", path)
        self.assertEqual(status, 200)
        self.assertEqual(body, f"gitea {path}")

    def test_npm_prefix_stripped_for_verdaccio(self):
        status, body, _ = self._raw("GET", "/npm/left-pad")
        self.assertEqual(status, 200)
        self.assertEqual(body, "verdaccio /left-pad")

    def test_npm_root_redirects(self):
        status, _, headers = self._raw("GET", "/npm")
        self.assertEqual(status, 301)
        self.assertEqual(headers["Location"], "/npm/")  # relative: keeps :8080

    def test_pypi_unauthenticated_gets_basic_challenge(self):
        status, _, headers = self._raw("GET", "/pypi/simple/six/")
        self.assertEqual(status, 401)
        self.assertEqual(headers.get("WWW-Authenticate"), 'Basic realm="Artea"')

    def test_pypi_bad_credentials_rejected(self):
        status, _, headers = self._raw("GET", "/pypi/simple/six/", auth=BAD_AUTH)
        self.assertEqual(status, 401)
        self.assertIn("WWW-Authenticate", headers)

    def test_pypi_private_served_from_gitea_only(self):
        status, body, _ = self._raw("GET", "/pypi/simple/private-pkg/", auth=GOOD_AUTH)
        self.assertEqual(status, 200)
        self.assertEqual(body, "gitea-simple private-pkg")
        # the precedence guarantee: devpi never consulted for a private name
        self.assertFalse([p for p in self.seen("devpi") if "private-pkg" in p])

    def test_pypi_404_falls_through_to_devpi_constrained(self):
        status, body, _ = self._raw("GET", "/pypi/simple/six/", auth=GOOD_AUTH)
        self.assertEqual(status, 200)
        self.assertEqual(body, "devpi /root/constrained/+simple/six/")
        # Gitea really was asked first, under the org pypi endpoint
        self.assertIn("/api/packages/artea/pypi/simple/six/", self.seen("gitea"))

    def test_devpi_file_downloads_guarded_and_passed_through(self):
        path = "/root/constrained/+f/abc/six.whl"
        status, _, _ = self._raw("GET", path)
        self.assertEqual(status, 401)
        status, body, _ = self._raw("GET", path, auth=GOOD_AUTH)
        self.assertEqual(status, 200)
        self.assertEqual(body, f"devpi {path}")

    def test_auth_result_cached(self):
        for _ in range(3):
            self._raw("GET", "/pypi/simple/six/", auth=GOOD_AUTH)
        hits = [a for p, a in self.upstreams["gitea"].requests
                if p == "/api/v1/user" and a == GOOD_AUTH]
        self.assertEqual(len(hits), 1, "valid credential should be cached for 30s")


if __name__ == "__main__":
    unittest.main(verbosity=2)
