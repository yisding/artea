"""Shared in-process HTTP stub lifecycle for the policy-sync test suite.

Every mock upstream (MockGitea, MockDevpi, MockOsv, _Stub) needs the identical
socket/thread plumbing: bind an ephemeral ThreadingHTTPServer on loopback, run
serve_forever on a short-poll daemon thread (so teardown returns quickly), and
expose url/start/stop. This module owns ONLY that lifecycle plus a no-op access
log and a small reply helper; each mock keeps its own Handler, capture shape,
auth, and fail flags.
"""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _StubHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # match BaseHTTPRequestHandler, stay quiet
        pass


def reply(handler, code, body, content_type="application/json"):
    """Write a complete response (status + Content-Type + Content-Length + body).

    Lifted from test_enrich's _reply; content_type is overridable (not hardcoded
    to application/json) so HTML/plain-text stub routes can use it too.
    """
    data = body.encode() if isinstance(body, str) else body
    handler.send_response(code)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


class StubServer:
    """Lifecycle base: ephemeral loopback ThreadingHTTPServer on a daemon thread.

    Subclasses set up their own state, then call ``super().__init__()`` and return
    their request Handler class from ``_build_handler``. The Handler should
    subclass ``_StubHandler`` to inherit the no-op access log.
    """

    def __init__(self):
        self.requests: list = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._build_handler())
        self.httpd.daemon_threads = True
        # short poll so shutdown() in teardown returns quickly
        self.thread = threading.Thread(
            target=lambda: self.httpd.serve_forever(poll_interval=0.01), daemon=True
        )

    def _build_handler(self) -> type[BaseHTTPRequestHandler]:
        raise NotImplementedError

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
