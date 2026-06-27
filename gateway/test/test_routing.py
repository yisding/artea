# Copyright 2026 The Artea Authors. All rights reserved.
#
# Functional test for the gateway nginx.conf: runs a real nginx (must be on PATH)
# on a loopback port against stdlib stub upstreams, then asserts the routing
# contract from docs/ARCHITECTURE.md. Works with unittest or pytest:
#
#   python3 gateway/test/test_routing.py
#
# The config is single-sourced as a Helm template (deploy/helm/artea/files/
# gateway/nginx.conf); this test renders it straight out of the chart with
# `helm template ... | yq`, so helm + yq must also be on PATH. The only edits
# made to the rendered config are filesystem paths, the listen port, and the
# four upstream host:port values (the chart's cluster Service names, swapped for
# loopback stubs). All routing/rewrite/auth logic is exercised unmodified.

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

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
RENDER_CHART_FILE = REPO_ROOT / "scripts" / "render-chart-file.sh"
NJS_DIR = REPO_ROOT / "gateway" / "njs"
TEST_NAMESPACE = "acme"

GOOD_PAT = "good-pat"

# Far larger than nginx's default one-page subrequest_output_buffer_size (4k/8k).
# The njs orchestrator buffers every subrequest response whole, so a realistic
# enriched document (or a private simple page with many files) must not overflow
# that buffer — which would make nginx abort with "too big subrequest response"
# and reset the client. A popular project with many releases easily produces a
# multi-hundred-KB-to-MB Simple-API document, so exercise a full 1 MB body here
# (the kind-e2e hits this with real packages: six ~14KB, urllib3 ~46KB).
BIG_BODY_PAD = "x" * (1024 * 1024)


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

    def log_message(self, format, *args):  # match BaseHTTPRequestHandler
        pass

    def _reply(self, code, body, headers=()):
        data = body.encode()
        self.send_response(code)
        header_keys = {k.lower() for k, _ in headers}
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        if "content-type" not in header_keys:
            self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        server = self.server
        auth = self.headers.get("Authorization")
        server.requests.append((self.path, auth))
        if server.tag == "gitea":
            # Echo the proxy-set forwarded headers so the routing test can assert
            # the gateway derives them from the client-facing scheme (a fronting
            # TLS-terminating ELB/ALB's X-Forwarded-Proto), not nginx's own
            # connection $scheme. Reached via the catch-all `location /`.
            if self.path == "/forwarded-echo":
                self._reply(200, "proto=%s outside=%s" % (
                    self.headers.get("X-Forwarded-Proto"),
                    self.headers.get("X-outside-url")))
                return
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
            # A private package whose PEP 503 page is large: the JSON probe
            # buffers it whole in njs, so it exercises the probe's subrequest
            # buffer (a too-small buffer would reset the connection here).
            if self.path.startswith(f"/api/packages/{TEST_NAMESPACE}/pypi/simple/big-private"):
                self._reply(200, "gitea-simple big-private " + BIG_BODY_PAD)
                return
            # Fail-closed probe outcomes (PEP 700 JSON path, pep700.js L74-82):
            # a Gitea outage (5xx) on the simple-probe must surface as a gateway
            # 502 and must NOT fall through to the public mirror for a possibly-
            # private name (dependency-confusion guard); an authorization answer
            # (403) for an existing private package must be relayed verbatim.
            if self.path.startswith(f"/api/packages/{TEST_NAMESPACE}/pypi/simple/probe-5xx"):
                self._reply(500, "gitea boom")
                return
            if self.path.startswith(f"/api/packages/{TEST_NAMESPACE}/pypi/simple/probe-forbidden"):
                self._reply(403, "forbidden by gitea")
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
        if server.tag == "policy-sync":
            # PEP 700 enrichment endpoint: echo the upstream+name so the routing
            # test can assert WHICH branch (gitea vs devpi) the njs orchestrator
            # chose, and that the v1+json content-type is relayed to the client.
            if self.path.startswith("/pypi/simple-enrich"):
                if "name=missing-private" in self.path:
                    # private package vanished between probe and enrich (race):
                    # policy-sync 404s the gitea branch -> gateway retries devpi.
                    if "upstream=gitea" in self.path:
                        self._reply(404, "no such private package")
                        return
                if "name=absent-everywhere" in self.path:
                    # absent from Gitea AND the public mirror: enrich_devpi raises
                    # EnrichNotFound -> policy-sync 404s -> the gateway must surface
                    # a real 404 (no candidates), not a 502.
                    self._reply(404, "no such project")
                    return
                # A realistic enriched document is tens-to-hundreds of KB; return
                # one well past nginx's default subrequest buffer so a too-small
                # buffer regresses here (the v1+json relay overflows and resets the
                # client) rather than only in the kind-e2e.
                body = ('{"meta":{"api-version":"1.1"},"name":"x","files":[],'
                        '"versions":[],"_pad":"' + BIG_BODY_PAD + '"}')
                self._reply(200, body,
                            headers=(("Content-Type", "application/vnd.pypi.simple.v1+json"),))
                return
        self._reply(200, f"{server.tag} {self.path}")

    do_POST = do_GET
    do_PUT = do_GET


class GatewayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for tool in ("nginx", "helm", "yq"):
            if shutil.which(tool) is None:
                raise unittest.SkipTest(
                    f"{tool} not on PATH (the gateway config renders via helm)")
        load_module = njs_load_directive()
        if load_module is None:
            raise unittest.SkipTest("host nginx lacks the njs module; use the "
                                    "docker validation from gateway/README.md")
        cls.tmp = tempfile.TemporaryDirectory(prefix="artea-gw-test.")
        tmp = pathlib.Path(cls.tmp.name)
        (tmp / "logs").mkdir()
        (tmp / "cache").mkdir()

        cls.upstreams = {t: Upstream(t) for t in ("gitea", "verdaccio", "devpi", "policy-sync")}
        for up in cls.upstreams.values():
            threading.Thread(target=up.serve_forever, daemon=True).start()

        cls.port = free_port()
        # The nginx.conf is single-sourced as a Helm template; render it out of
        # the chart for the test namespace (scripts/render-chart-file.sh).
        render = subprocess.run(
            [str(RENDER_CHART_FILE), "templates/gateway.yaml", "nginx.conf",
             "--set", f"global.privateNamespace={TEST_NAMESPACE}"],
            capture_output=True, text=True,
        )
        if render.returncode != 0:
            raise RuntimeError(f"render-chart-file.sh failed:\n{render.stderr}")
        conf = render.stdout
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
            # the chart's cluster Service names (gateway.upstreams.*), swapped
            # for the loopback stub upstreams
            "artea-gitea-http:3000": "127.0.0.1:%d" % cls.upstreams["gitea"].server_port,
            "artea-verdaccio:4873": "127.0.0.1:%d" % cls.upstreams["verdaccio"].server_port,
            "artea-devpi:3141": "127.0.0.1:%d" % cls.upstreams["devpi"].server_port,
            "artea-policy-sync:8920": "127.0.0.1:%d" % cls.upstreams["policy-sync"].server_port,
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
    def _raw(cls, method, path, auth=None, accept=None, headers=None):
        """http.client keeps the path byte-exact (no normalization)."""
        conn = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=5)
        headers = dict(headers) if headers else {}
        if auth:
            headers["Authorization"] = auth
        if accept:
            headers["Accept"] = accept
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

    def test_forwarded_proto_honors_tls_terminating_proxy(self):
        # Behind an ELB/ALB that terminates TLS and forwards over HTTP on :80,
        # the gateway must pass the CLIENT-facing scheme downstream (from the
        # ALB's X-Forwarded-Proto: https), not nginx's own http $scheme — else
        # Gitea emits http:// 301/302 redirects and devpi http:// file links.
        status, body, _ = self._raw(
            "GET", "/forwarded-echo", headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(status, 200)
        self.assertIn("proto=https", body)
        self.assertIn("outside=https://", body)

    def test_forwarded_proto_uses_trusted_last_value_in_a_chain(self):
        # When a proxy APPENDS rather than overwrites, X-Forwarded-Proto arrives
        # as a comma list. The directly-fronting (trusted) proxy's value is the
        # LAST element — a client can only prepend — so the gateway must select
        # the last token, never the client-controlled first one. This blocks a
        # client from smuggling a downgraded scheme past an HTTPS ingress: with
        # "https, http" the real (last) scheme http wins, and a spoofed leading
        # "http" cannot mask the trusted trailing "https".
        for header, want in (("http, https", "https"), ("https, http", "http")):
            status, body, _ = self._raw(
                "GET", "/forwarded-echo",
                headers={"X-Forwarded-Proto": header})
            self.assertEqual(status, 200, header)
            self.assertIn(f"proto={want}", body, header)
            self.assertIn(f"outside={want}://", body, header)

    def test_forwarded_proto_falls_back_to_connection_scheme(self):
        # No fronting proxy (direct connection / kubectl port-forward): fall back
        # to nginx's own $scheme, which is http for this loopback test client.
        status, body, _ = self._raw("GET", "/forwarded-echo")
        self.assertEqual(status, 200)
        self.assertIn("proto=http", body)
        self.assertIn("outside=http://", body)
        # An unexpected token (not exactly http|https) is ignored rather than
        # forwarded verbatim into a scheme — same safe fallback to $scheme.
        status, body, _ = self._raw(
            "GET", "/forwarded-echo", headers={"X-Forwarded-Proto": "javascript"})
        self.assertEqual(status, 200)
        self.assertIn("proto=http", body)
        self.assertIn("outside=http://", body)
        self.assertIn("outside=http://", body)

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

    # ---- PEP 700 JSON enrichment routing (Accept: ...v1+json) ----

    JSON_ACCEPT = "application/vnd.pypi.simple.v1+json"

    def test_pypi_json_private_enriched_via_gitea_branch(self):
        # JSON-Accept + a private name: the njs orchestrator probes Gitea (200),
        # then enriches via policy-sync upstream=gitea; the v1+json body is
        # relayed with the right content-type, and devpi is never consulted.
        status, body, headers = self._raw("GET", "/pypi/simple/private-pkg/",
                                          auth=GOOD_AUTH, accept=self.JSON_ACCEPT)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), self.JSON_ACCEPT)
        self.assertIn('"api-version":"1.1"', body)
        enrich = [p for p in self.seen("policy-sync") if "upstream=gitea" in p and "private-pkg" in p]
        self.assertTrue(enrich, "expected a policy-sync gitea-branch enrich call")
        self.assertFalse([p for p in self.seen("devpi") if "private-pkg" in p])

    def test_pypi_json_public_enriched_via_devpi_branch(self):
        # JSON-Accept + a public-only name: Gitea probe 404s -> enrich devpi.
        status, body, headers = self._raw("GET", "/pypi/simple/six/",
                                          auth=GOOD_AUTH, accept=self.JSON_ACCEPT)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), self.JSON_ACCEPT)
        self.assertIn('"api-version":"1.1"', body)
        self.assertTrue([p for p in self.seen("policy-sync") if "upstream=devpi" in p and "name=six" in p])
        # the Gitea-first probe really ran (precedence preserved on the JSON path)
        self.assertIn(f"/api/packages/{TEST_NAMESPACE}/pypi/simple/six/", self.seen("gitea"))

    def test_pypi_json_gitea_branch_enrich_404_falls_through_to_devpi(self):
        # Race: Gitea probe 200 but the package vanished by enrich time; the
        # gitea-branch enrich 404s and the orchestrator retries the devpi branch.
        status, body, headers = self._raw("GET", "/pypi/simple/missing-private/",
                                          auth=GOOD_AUTH, accept=self.JSON_ACCEPT)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), self.JSON_ACCEPT)
        self.assertTrue([p for p in self.seen("policy-sync") if "upstream=devpi" in p and "missing-private" in p])

    def test_pypi_json_large_documents_not_truncated_by_subrequest_buffer(self):
        # Regression (kind-e2e S8/S10/S15): the njs orchestrator relays the
        # enriched body via an in-memory subrequest, and the Gitea-branch probe
        # buffers the full PEP 503 page the same way. A real enriched document /
        # private page is far larger than nginx's default 4k/8k page; without a
        # raised subrequest_output_buffer_size nginx aborts with "too big
        # subrequest response" and resets the connection (pip sees
        # RemoteDisconnected). Both branches must serve the large body intact.
        for name, marker in (("six", "upstream=devpi"), ("big-private", "upstream=gitea")):
            status, body, headers = self._raw("GET", f"/pypi/simple/{name}/",
                                              auth=GOOD_AUTH, accept=self.JSON_ACCEPT)
            self.assertEqual(status, 200, name)
            self.assertEqual(headers.get("Content-Type"), self.JSON_ACCEPT, name)
            self.assertIn('"api-version":"1.1"', body, name)
            self.assertGreater(len(body), 1024 * 1024, name)  # full 1 MB body relayed
            self.assertTrue([p for p in self.seen("policy-sync")
                             if marker in p and name in p], name)

    def test_pypi_json_devpi_branch_404_propagates_as_404(self):
        # A name absent from both Gitea and the public mirror: the Gitea probe
        # 404s, the devpi-branch enrich 404s, and the gateway must surface a real
        # 404 ("no candidates") — not mask it as a 502 that JSON pip/uv would
        # retry as a transient index failure. Matches the uncached HTML path.
        status, _, _ = self._raw("GET", "/pypi/simple/absent-everywhere/",
                                 auth=GOOD_AUTH, accept=self.JSON_ACCEPT)
        self.assertEqual(status, 404)
        self.assertTrue([p for p in self.seen("policy-sync")
                         if "upstream=devpi" in p and "absent-everywhere" in p])

    def test_pypi_json_gitea_probe_5xx_fails_closed_to_502(self):
        # Fail-closed (pep700.js L74/L81): a Gitea outage on the simple-probe
        # (5xx) is neither a 200 (private hit) nor a 404 (public fallthrough).
        # For a name that might be private, falling through to the public mirror
        # would be a dependency-confusion leak, so the orchestrator returns a
        # gateway 502 and NEVER consults devpi or the policy-sync public branch.
        ps_before = len(self.upstreams["policy-sync"].requests)
        status, _, _ = self._raw("GET", "/pypi/simple/probe-5xx/",
                                 auth=GOOD_AUTH, accept=self.JSON_ACCEPT)
        self.assertEqual(status, 502)
        # the probe really hit Gitea (precedence ran) ...
        self.assertIn(f"/api/packages/{TEST_NAMESPACE}/pypi/simple/probe-5xx/",
                      self.seen("gitea"))
        # ... but the possibly-private name never reached the public mirror or
        # any policy-sync enrich (no silent public fallthrough on a Gitea 5xx).
        self.assertFalse([p for p in self.seen("devpi") if "probe-5xx" in p])
        self.assertFalse([p for p in self.seen("policy-sync") if "probe-5xx" in p])
        self.assertEqual(len(self.upstreams["policy-sync"].requests), ps_before)

    def test_pypi_json_gitea_probe_403_relayed_not_public(self):
        # Fail-closed (pep700.js L81): a 403 from the simple-probe is Gitea's
        # real authorization answer for an existing private package and must be
        # relayed verbatim — not masked as a 502, and never softened into a
        # 200/public fallthrough that would leak a private name to devpi.
        ps_before = len(self.upstreams["policy-sync"].requests)
        status, _, _ = self._raw("GET", "/pypi/simple/probe-forbidden/",
                                 auth=GOOD_AUTH, accept=self.JSON_ACCEPT)
        self.assertEqual(status, 403)
        self.assertIn(f"/api/packages/{TEST_NAMESPACE}/pypi/simple/probe-forbidden/",
                      self.seen("gitea"))
        self.assertFalse([p for p in self.seen("devpi") if "probe-forbidden" in p])
        self.assertFalse([p for p in self.seen("policy-sync") if "probe-forbidden" in p])
        self.assertEqual(len(self.upstreams["policy-sync"].requests), ps_before)

    def test_pypi_non_json_accept_unchanged_gitea_first_fallthrough(self):
        # Regression guard: WITHOUT the JSON Accept header the existing
        # Gitea-first/404-fallback path is byte-identical and policy-sync is
        # never touched.
        before = len(self.upstreams["policy-sync"].requests)
        status, body, _ = self._raw("GET", "/pypi/simple/six/", auth=GOOD_AUTH)
        self.assertEqual(status, 200)
        self.assertIn("six-1.0.0-py3-none-any.whl", body)  # the HTML page, not JSON
        status, body, _ = self._raw("GET", "/pypi/simple/private-pkg/", auth=GOOD_AUTH)
        self.assertEqual((status, body), (200, "gitea-simple private-pkg"))
        self.assertEqual(len(self.upstreams["policy-sync"].requests), before,
                         "non-JSON requests must never reach policy-sync")

    def test_pypi_json_requires_auth(self):
        # the JSON enrichment path is gated by the same auth_request guard
        status, _, headers = self._raw("GET", "/pypi/simple/six/", accept=self.JSON_ACCEPT)
        self.assertEqual(status, 401)
        self.assertEqual(headers.get("WWW-Authenticate"), 'Basic realm="Artea"')

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
