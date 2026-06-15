# Copyright 2026 The Artea Authors. All rights reserved.
#
# Functional test for gateway/nginx.conf.template: runs a real nginx (must be on PATH)
# on a loopback port against stdlib stub upstreams, then asserts the routing
# contract from docs/ARCHITECTURE.md. Stdlib-only; works with unittest or pytest:
#
#   python3 gateway/test/test_routing.py
#
# The only edits made to the config under test are filesystem paths, the listen
# port, and the three upstream host:port values (hostnames only resolve inside
# docker compose). All routing/rewrite/auth logic is exercised unmodified.

import base64
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

CONF = pathlib.Path(__file__).resolve().parent.parent / "nginx.conf.template"
NJS_DIR = CONF.parent / "njs"
TEST_NAMESPACE = "acme"

GOOD_PAT = "good-pat"


def basic_auth(user, token):
    encoded = base64.b64encode(f"{user}:{token}".encode()).decode()
    return "Basic " + encoded


GOOD_AUTH = basic_auth("user", GOOD_PAT)
GOOD_TOKEN_AUTH = "Bearer " + GOOD_PAT
BAD_AUTH = basic_auth("user", "revoked")
NONMEMBER_AUTH = basic_auth("outsider", GOOD_PAT)
NO_PACKAGE_AUTH = basic_auth("user", "no-package")

# Where distros/images put nginx's dynamic modules; the conf's load_module path
# is relative to the prefix (-p), so the test needs an absolute host path.
NJS_MODULE_DIRS = (
    "/usr/lib/nginx/modules",
    "/usr/lib64/nginx/modules",
    "/usr/local/lib/nginx/modules",
    "/opt/homebrew/lib/nginx/modules",
    "/etc/nginx/modules",
)


def njs_load_directive():
    """Host replacement for the conf's load_module line.

    Returns the directive, '' when njs is compiled into the binary, or None
    when the host nginx cannot provide njs at all (test must skip; use the
    docker validation from gateway/README.md instead).
    """
    for d in NJS_MODULE_DIRS:
        path = pathlib.Path(d) / "ngx_http_js_module.so"
        if path.exists():
            return f"load_module {path};"
    info = subprocess.run(["nginx", "-V"], capture_output=True, text=True)
    if "njs" in info.stderr or "http_js" in info.stderr:
        return ""  # statically built in; no load_module needed
    return None


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
                if auth in (GOOD_AUTH, GOOD_TOKEN_AUTH, NO_PACKAGE_AUTH):
                    self._reply(200, '{"login":"user"}')
                elif auth == NONMEMBER_AUTH:
                    self._reply(200, '{"login":"outsider"}')
                else:
                    self._reply(401, "unauthorized")
                return
            if self.path.startswith(f"/api/v1/orgs/{TEST_NAMESPACE}/members/"):
                if auth in (GOOD_AUTH, GOOD_TOKEN_AUTH, NO_PACKAGE_AUTH):
                    if self.path == f"/api/v1/orgs/{TEST_NAMESPACE}/members/user":
                        self._reply(204, "")
                    else:
                        self._reply(404, "not found")
                elif auth == NONMEMBER_AUTH:
                    self._reply(404, "not found")
                else:
                    self._reply(401, "unauthorized")
                return
            if self.path == f"/api/v1/packages/{TEST_NAMESPACE}/?type=pypi&limit=1":
                if auth in (GOOD_AUTH, GOOD_TOKEN_AUTH):
                    self._reply(200, "[]")
                elif auth == NO_PACKAGE_AUTH:
                    self._reply(403, "missing package scope")
                else:
                    self._reply(401, "unauthorized")
                return
            # private package exists; "six" does not (mirrors Gitea's 404)
            if self.path.startswith(f"/api/packages/{TEST_NAMESPACE}/pypi/simple/private-pkg"):
                self._reply(200, "gitea-simple private-pkg")
                return
            if self.path.startswith(f"/api/packages/{TEST_NAMESPACE}/pypi/simple/"):
                if auth not in (GOOD_AUTH, GOOD_TOKEN_AUTH):
                    self._reply(401, "unauthorized")
                    return
                self._reply(404, "package does not exist")
                return
        if server.tag == "verdaccio":
            # stand-in for the policy middleware's tarball block (S13)
            if self.path.endswith("/blocked-1.0.0.tgz"):
                self._reply(403, "blocked by policy")
                return
        if server.tag == "devpi":
            if self.path == "/root/constrained/+simple/six/":
                filler = "\n".join(f'<a href="#filler-{i}">filler</a>' for i in range(2000))
                body = (
                    filler
                    + '\n<a href="http://localhost:8080/root/pypi/%2Bf/472/1f391ed90541f/'
                    + 'six-1.0.0-py3-none-any.whl#sha256=abc">six</a>'
                )
                self._reply(200, body)
                return
            if self.path.startswith("/+artea/file-allowed?path="):
                if "six-1.0.0-py3-none-any.whl" in self.path:
                    self._reply(204, "")
                else:
                    self._reply(403, "blocked by constraints")
                return
            # devpi should never need to redirect (the gateway sends canonical
            # names), but if it does, the gateway must map it back to /pypi/
            if self.path == "/root/constrained/+simple/redirector/":
                loc = "http://localhost:8080/root/constrained/+simple/target/"
                self._reply(302, "", headers=(("Location", loc),))
                return
        self._reply(200, f"{server.tag} {self.path}")

    do_POST = do_GET
    do_PUT = do_GET


class GatewayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which("nginx") is None:
            raise unittest.SkipTest("nginx binary not on PATH")
        load_module = njs_load_directive()
        if load_module is None:
            raise unittest.SkipTest("host nginx lacks the njs module; use the "
                                    "docker validation from gateway/README.md")
        cls.tmp = tempfile.TemporaryDirectory(prefix="artea-gw-test.")
        tmp = pathlib.Path(cls.tmp.name)
        (tmp / "logs").mkdir()
        (tmp / "cache").mkdir()

        cls.upstreams = {t: Upstream(t) for t in ("gitea", "verdaccio", "devpi")}
        for up in cls.upstreams.values():
            threading.Thread(target=up.serve_forever, daemon=True).start()

        cls.port = free_port()
        conf = CONF.read_text()
        conf = conf.replace("__ARTEA_NAMESPACE__", TEST_NAMESPACE)
        # nginx's compiled-in temp dirs (client_body, proxy, ...) often live in
        # root-owned /var/cache/nginx; point them into the test tmpdir so the
        # suite runs as any user with any nginx build.
        temp_dirs = "\n".join(
            f"    {d}_temp_path {tmp / 'cache' / d};"
            for d in ("client_body", "proxy", "fastcgi", "uwsgi", "scgi"))
        subs = {
            "listen 80;": f"listen 127.0.0.1:{cls.port};",
            "load_module modules/ngx_http_js_module.so;": load_module,
            "http {": "http {\n" + temp_dirs,
            "/etc/nginx/njs": str(NJS_DIR),
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
        path = f"/api/packages/{TEST_NAMESPACE}/npm/@{TEST_NAMESPACE}%2fhello"
        status, body, _ = self._raw("GET", path, auth=GOOD_TOKEN_AUTH)
        self.assertEqual(status, 200)
        self.assertEqual(body, f"gitea {path}")

    def test_gitea_package_api_limited_to_artea_npm_and_pypi(self):
        before = len(self.upstreams["gitea"].requests)
        for path in (
            "/api/packages/dev1/npm/left-pad",
            "/api/packages/other-org/pypi/simple/six/",
            f"/api/packages/{TEST_NAMESPACE}/rubygems/gems/foo",
            f"/api/packages/{TEST_NAMESPACE}/container/v2/foo/manifests/latest",
        ):
            status, body, _ = self._raw("GET", path)
            self.assertEqual((status, body), (404, "not found\n"), path)
        self.assertEqual(len(self.upstreams["gitea"].requests), before)

    def test_gitea_package_api_encoded_prefix_variants_hidden(self):
        before = len(self.upstreams["gitea"].requests)
        for path in (
            f"/api%2fpackages/{TEST_NAMESPACE}/npm/@{TEST_NAMESPACE}%2fhello",
            f"/api/packages%2f{TEST_NAMESPACE}/npm/@{TEST_NAMESPACE}%2fhello",
            f"/api/packages/{TEST_NAMESPACE}%2fnpm/@{TEST_NAMESPACE}%2fhello",
            f"/api/packages/{TEST_NAMESPACE}/npm%2f@{TEST_NAMESPACE}%2fhello",
            f"/api/packages/{TEST_NAMESPACE}/pypi%2fsimple/six/",
        ):
            status, body, _ = self._raw("GET", path, auth=GOOD_TOKEN_AUTH)
            self.assertEqual((status, body), (404, "not found\n"), path)
        self.assertEqual(len(self.upstreams["gitea"].requests), before)

    def test_gitea_package_api_requires_org_and_package_scope(self):
        paths = (
            f"/api/packages/{TEST_NAMESPACE}/npm/@{TEST_NAMESPACE}%2fhello",
            f"/api/packages/{TEST_NAMESPACE}/pypi/files/six/1.0.0/six-1.0.0-py3-none-any.whl",
        )
        for path in paths:
            before = len([p for p in self.seen("gitea") if p == path])
            status, _, headers = self._raw("GET", path)
            self.assertEqual(status, 401, path)
            self.assertEqual(headers.get("WWW-Authenticate"), 'Basic realm="Artea"')
            status, _, headers = self._raw("GET", path, auth=NONMEMBER_AUTH)
            self.assertEqual(status, 403, path)
            self.assertNotIn("WWW-Authenticate", headers)
            status, _, headers = self._raw("GET", path, auth=NO_PACKAGE_AUTH)
            self.assertEqual(status, 403, path)
            self.assertNotIn("WWW-Authenticate", headers)
            self.assertEqual(len([p for p in self.seen("gitea") if p == path]), before)

            status, body, _ = self._raw("GET", path, auth=GOOD_TOKEN_AUTH)
            self.assertEqual(status, 200, path)
            self.assertEqual(body, f"gitea {path}")

    def test_npm_prefix_stripped_for_verdaccio(self):
        status, body, _ = self._raw("GET", "/npm/left-pad", auth=GOOD_AUTH)
        self.assertEqual(status, 200)
        self.assertEqual(body, "verdaccio /left-pad")
        # credential must reach Verdaccio: its auth plugin authorizes npm-level
        self.assertIn(("/left-pad", GOOD_AUTH), self.upstreams["verdaccio"].requests)

    def test_npm_anonymous_rejected_everywhere(self):
        # Verdaccio's service endpoints must not answer without credentials
        # ("anonymous access: none, anywhere")
        before = len(self.upstreams["verdaccio"].requests)
        for path in ("/npm/-/ping", "/npm/-/v1/search?text=left-pad",
                     "/npm/-/npm/v1/security/audits/quick", "/npm/left-pad"):
            status, _, headers = self._raw("GET", path)
            self.assertEqual(status, 401, path)
            self.assertEqual(headers.get("WWW-Authenticate"), 'Basic realm="Artea"')
        self.assertEqual(len(self.upstreams["verdaccio"].requests), before)

    def test_npm_bad_credentials_rejected(self):
        status, _, headers = self._raw("GET", "/npm/-/ping", auth=BAD_AUTH)
        self.assertEqual(status, 401)
        self.assertIn("WWW-Authenticate", headers)

    def test_npm_missing_package_scope_rejected(self):
        before = len(self.upstreams["verdaccio"].requests)
        status, body, headers = self._raw("GET", "/npm/left-pad", auth=NO_PACKAGE_AUTH)
        self.assertEqual((status, body), (403, "forbidden\n"))
        self.assertNotIn("WWW-Authenticate", headers)
        self.assertEqual(len(self.upstreams["verdaccio"].requests), before)

    def test_package_scope_probe_uses_gitea_management_api(self):
        status, _, _ = self._raw("GET", "/npm/left-pad", auth=GOOD_AUTH)
        self.assertEqual(status, 200)
        self.assertIn(
            (f"/api/v1/packages/{TEST_NAMESPACE}/?type=pypi&limit=1", GOOD_AUTH),
            self.upstreams["gitea"].requests,
        )

    def test_npm_upstream_403_passes_through(self):
        # Verdaccio's own 401/403 (policy tarball block, S13) must NOT be
        # re-mapped to the gateway's Basic challenge — only auth_request
        # failures are (proxy_intercept_errors stays off on /npm/).
        status, body, _ = self._raw("GET", "/npm/blocked/-/blocked-1.0.0.tgz",
                                    auth=GOOD_AUTH)
        self.assertEqual((status, body), (403, "blocked by policy"))

    def test_npm_root_redirects(self):
        status, _, headers = self._raw("GET", "/npm")
        self.assertEqual(status, 301)
        self.assertEqual(headers["Location"], "/npm/")  # relative: keeps :8080

    def test_npm_private_scope_to_gitea_raw_encoding_preserved(self):
        # gateway scope routing: the %2f/%40 encodings npm sends must reach
        # Gitea byte-for-byte after the gateway guard proves org/package scope.
        for raw in (f"/npm/@{TEST_NAMESPACE}%2fscoped-a", f"/npm/%40{TEST_NAMESPACE}%2Fscoped-a"):
            status, body, _ = self._raw("GET", raw, auth=GOOD_AUTH)
            self.assertEqual(status, 200, raw)
            self.assertEqual(body, f"gitea /api/packages/{TEST_NAMESPACE}/npm{raw[4:]}", raw)
        self.assertFalse([p for p in self.seen("verdaccio") if "scoped-a" in p])

    def test_npm_private_scope_requires_org_and_package_scope(self):
        path = f"/npm/@{TEST_NAMESPACE}%2fscoped-auth"
        g_before = len([p for p in self.seen("gitea") if "scoped-auth" in p])
        v_before = len([p for p in self.seen("verdaccio") if "scoped-auth" in p])

        status, _, headers = self._raw("GET", path)
        self.assertEqual(status, 401)
        self.assertEqual(headers.get("WWW-Authenticate"), 'Basic realm="Artea"')

        status, body, headers = self._raw("GET", path, auth=NONMEMBER_AUTH)
        self.assertEqual((status, body), (403, "forbidden\n"))
        self.assertNotIn("WWW-Authenticate", headers)

        status, body, headers = self._raw("GET", path, auth=NO_PACKAGE_AUTH)
        self.assertEqual((status, body), (403, "forbidden\n"))
        self.assertNotIn("WWW-Authenticate", headers)

        self.assertEqual(len([p for p in self.seen("gitea") if "scoped-auth" in p]), g_before)
        self.assertEqual(len([p for p in self.seen("verdaccio") if "scoped-auth" in p]), v_before)

    def test_npm_private_scope_case_variants_to_gitea_never_verdaccio(self):
        # case variants must never reach Verdaccio (its private-scope deny is
        # case-sensitive) and from there the npmjs uplink: the scope match is
        # case-insensitive, so case variants land on Gitea (404 there in life)
        for raw in (f"/npm/@{TEST_NAMESPACE.upper()}%2Fscoped-c", f"/npm/@Acme/scoped-c"):
            status, body, _ = self._raw("GET", raw, auth=GOOD_AUTH)
            self.assertEqual(status, 200, raw)
            self.assertEqual(body, f"gitea /api/packages/{TEST_NAMESPACE}/npm{raw[4:]}", raw)
        self.assertFalse([p for p in self.seen("verdaccio")
                          if "scoped-c" in p.lower()])

    def test_npm_private_scope_dist_tag_api_to_gitea(self):
        path = f"/npm/-/package/@{TEST_NAMESPACE}%2fscoped-b/dist-tags"
        status, body, _ = self._raw("GET", path, auth=GOOD_AUTH)
        self.assertEqual(status, 200)
        self.assertEqual(
            body, f"gitea /api/packages/{TEST_NAMESPACE}/npm/-/package/@{TEST_NAMESPACE}%2fscoped-b/dist-tags")
        self.assertFalse([p for p in self.seen("verdaccio") if "scoped-b" in p])

    def test_npm_private_scope_publish_put_routes_identically(self):
        status, body, _ = self._raw("PUT", f"/npm/@{TEST_NAMESPACE}%2fscoped-d", auth=GOOD_AUTH)
        self.assertEqual(status, 200)
        self.assertEqual(body, f"gitea /api/packages/{TEST_NAMESPACE}/npm/@{TEST_NAMESPACE}%2fscoped-d")

    def test_npm_scope_boundary_artea_evil_stays_on_verdaccio(self):
        # the required scope separator (%2f or /) is part of the regex:
        # lookalike scopes must not be captured by the Gitea route
        status, body, _ = self._raw("GET", f"/npm/@{TEST_NAMESPACE}-evil%2fnope", auth=GOOD_AUTH)
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(f"verdaccio /@{TEST_NAMESPACE}-evil"), body)
        self.assertFalse([p for p in self.seen("gitea") if f"{TEST_NAMESPACE}-evil" in p])

    def test_npm_percent_encoded_scope_letters_rejected_at_gateway(self):
        # a scoped-location match whose raw URI matches neither map pattern
        # fires the 400 guard at the gateway, reaching no upstream
        g_before = len(self.upstreams["gitea"].requests)
        v_before = len(self.upstreams["verdaccio"].requests)
        status, _, _ = self._raw("GET", "/npm/@%61cme/scoped-e", auth=GOOD_AUTH)
        self.assertEqual(status, 400)
        self.assertEqual(len(self.upstreams["gitea"].requests), g_before)
        self.assertEqual(len(self.upstreams["verdaccio"].requests), v_before)

    def test_npm_double_encoded_separator_rejected_at_gateway(self):
        # %252f normalizes once to literal '%2f' text and enters the scoped
        # location, but the raw URI is not an allowed map form: fail closed
        # before either upstream sees it.
        g_before = len(self.upstreams["gitea"].requests)
        v_before = len(self.upstreams["verdaccio"].requests)
        status, body, _ = self._raw("GET", f"/npm/@{TEST_NAMESPACE}%252fscoped-f",
                                    auth=GOOD_AUTH)
        self.assertEqual(status, 400)
        self.assertEqual(body, "")
        self.assertEqual(len(self.upstreams["gitea"].requests), g_before)
        self.assertEqual(len(self.upstreams["verdaccio"].requests), v_before)

    def test_pypi_unauthenticated_gets_basic_challenge(self):
        status, _, headers = self._raw("GET", "/pypi/simple/six/")
        self.assertEqual(status, 401)
        self.assertEqual(headers.get("WWW-Authenticate"), 'Basic realm="Artea"')

    def test_pypi_bad_credentials_rejected(self):
        status, _, headers = self._raw("GET", "/pypi/simple/six/", auth=BAD_AUTH)
        self.assertEqual(status, 401)
        self.assertIn("WWW-Authenticate", headers)

    def test_pypi_non_org_member_rejected(self):
        status, _, headers = self._raw("GET", "/pypi/simple/six/",
                                       auth=NONMEMBER_AUTH)
        self.assertEqual(status, 403)
        self.assertNotIn("WWW-Authenticate", headers)

    def test_pypi_missing_package_scope_rejected(self):
        before = len(self.upstreams["devpi"].requests)
        status, body, headers = self._raw("GET", "/pypi/simple/six/",
                                          auth=NO_PACKAGE_AUTH)
        self.assertEqual((status, body), (403, "forbidden\n"))
        self.assertNotIn("WWW-Authenticate", headers)
        self.assertEqual(len(self.upstreams["devpi"].requests), before)

    def test_pypi_private_served_from_gitea_only(self):
        status, body, _ = self._raw("GET", "/pypi/simple/private-pkg/", auth=GOOD_AUTH)
        self.assertEqual(status, 200)
        self.assertEqual(body, "gitea-simple private-pkg")
        # the precedence guarantee: devpi never consulted for a private name
        self.assertFalse([p for p in self.seen("devpi") if "private-pkg" in p])

    def test_pypi_404_falls_through_to_devpi_constrained_index(self):
        status, body, _ = self._raw("GET", "/pypi/simple/six/", auth=GOOD_AUTH)
        self.assertEqual(status, 200)
        self.assertIn("six-1.0.0-py3-none-any.whl", body)
        # Gitea really was asked first, under the org pypi endpoint
        self.assertIn(f"/api/packages/{TEST_NAMESPACE}/pypi/simple/six/", self.seen("gitea"))
        self.assertIn("/root/constrained/+simple/six/", self.seen("devpi"))

    def test_pypi_name_normalized_before_gitea_lookup(self):
        # S16: non-canonical spellings of a private name must still resolve to
        # the private package and never fall through to the public mirror.
        for spelling in ("Private-PKG", "private_pkg", "Private..pkg"):
            status, body, _ = self._raw("GET", f"/pypi/simple/{spelling}/",
                                        auth=GOOD_AUTH)
            self.assertEqual(status, 200, spelling)
            self.assertEqual(body, "gitea-simple private-pkg", spelling)
        self.assertFalse([p for p in self.seen("devpi") if "private" in p.lower()])

    def test_pypi_fallback_uses_normalized_name(self):
        status, body, _ = self._raw("GET", "/pypi/simple/Some_Public.Pkg/",
                                    auth=GOOD_AUTH)
        self.assertEqual(status, 200)
        self.assertEqual(body, "devpi /root/constrained/+simple/some-public-pkg/")
        self.assertIn(f"/api/packages/{TEST_NAMESPACE}/pypi/simple/some-public-pkg/",
                      self.seen("gitea"))

    def test_pypi_missing_slash_canonicalized_by_gateway(self):
        # the slashless form must go through the Gitea-first check itself; it
        # must never reach devpi slashless (devpi would answer with its own
        # redirect, letting redirect-following clients skip the check)
        status, body, _ = self._raw("GET", "/pypi/simple/six", auth=GOOD_AUTH)
        self.assertEqual(status, 200)
        self.assertIn("six-1.0.0-py3-none-any.whl", body)
        self.assertIn(f"/api/packages/{TEST_NAMESPACE}/pypi/simple/six/", self.seen("gitea"))
        self.assertNotIn("/root/constrained/+simple/six", self.seen("devpi"))

    def test_pypi_bare_index_guarded(self):
        status, _, _ = self._raw("GET", "/pypi/simple/")
        self.assertEqual(status, 401)
        status, body, _ = self._raw("GET", "/pypi/simple/", auth=GOOD_AUTH)
        self.assertEqual((status, body), (200, "devpi /root/constrained/+simple/"))

    def test_pypi_devpi_redirect_mapped_back_into_gateway(self):
        # belt-and-braces: a devpi Location header may never point the client
        # at /root/... directly — it must re-enter the precedence check
        status, _, headers = self._raw("GET", "/pypi/simple/redirector/",
                                       auth=GOOD_AUTH)
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/pypi/simple/target/")

    def test_devpi_file_downloads_guarded_and_passed_through(self):
        path = "/root/pypi/%2Bf/472/1f391ed90541f/six-1.0.0-py3-none-any.whl"
        status, _, _ = self._raw("GET", path)
        self.assertEqual(status, 401)
        status, body, _ = self._raw("GET", path, auth=GOOD_AUTH)
        self.assertEqual(status, 200)
        self.assertIn("six-1.0.0-py3-none-any.whl", body)
        self.assertTrue(any("six-1.0.0-py3-none-any.whl" in p
                            for p in self.seen("devpi")))
        self.assertTrue(any(p.startswith("/+artea/file-allowed?path=")
                            for p in self.seen("devpi")))

    def test_devpi_file_download_requires_current_constrained_link(self):
        path = "/root/pypi/%2Bf/472/1f391ed90541f/six-9.9.9-py3-none-any.whl"
        before = len([p for p in self.seen("devpi") if "six-9.9.9" in p])
        status, body, headers = self._raw("GET", path, auth=GOOD_AUTH)
        self.assertEqual((status, body), (403, "forbidden\n"))
        self.assertNotIn("WWW-Authenticate", headers)
        after = len([p for p in self.seen("devpi") if "six-9.9.9" in p])
        self.assertEqual(after, before, "blocked file must not be proxied to devpi")

    def test_devpi_file_download_missing_package_scope_rejected(self):
        path = "/root/pypi/%2Bf/472/1f391ed90541f/six-1.0.0-py3-none-any.whl"
        status, body, headers = self._raw("GET", path, auth=NO_PACKAGE_AUTH)
        self.assertEqual((status, body), (403, "forbidden\n"))
        self.assertNotIn("WWW-Authenticate", headers)

    def test_raw_devpi_simple_routes_not_publicly_reachable(self):
        before = len(self.upstreams["devpi"].requests)
        for path in (
            "/root/pypi/+simple/six/",
            "/root/constrained/+simple/six/",
            "/root/constrained/+simple/private-pkg/",
        ):
            status, body, _ = self._raw("GET", path, auth=GOOD_AUTH)
            self.assertEqual((status, body), (404, "not found\n"), path)
        self.assertEqual(len(self.upstreams["devpi"].requests), before)

    def test_auth_result_cached(self):
        for _ in range(3):
            self._raw("GET", "/pypi/simple/six/", auth=GOOD_AUTH)
        hits = [a for p, a in self.upstreams["gitea"].requests
                if p == "/api/v1/user" and a == GOOD_AUTH]
        self.assertEqual(len(hits), 1, "valid credential should be cached for 30s")


if __name__ == "__main__":
    unittest.main(verbosity=2)
