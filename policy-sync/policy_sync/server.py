"""HTTP surface: Gitea push-webhook receiver + health endpoint.

Stdlib http.server is enough here: two routes, internal-only traffic, and the
actual sync work runs on a single background worker thread (webhooks only set
an event, so deliveries return immediately and concurrent syncs are coalesced).
"""

import hmac
import hashlib
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import Config
from .sync import Syncer

log = logging.getLogger(__name__)

MAX_BODY = 10 * 1024 * 1024  # webhook payloads are small; cap reads defensively


class SyncState:
    """Last-sync status shared between the worker thread and /healthz."""

    def __init__(self):
        self._lock = threading.Lock()
        self.last_sync_ok: bool | None = None
        self.last_sync_at: float | None = None

    def record(self, ok: bool) -> None:
        with self._lock:
            self.last_sync_ok = ok
            self.last_sync_at = time.time()

    def snapshot(self) -> dict:
        with self._lock:
            return {"last_sync_ok": self.last_sync_ok, "last_sync_at": self.last_sync_at}


class PolicySyncHandler(BaseHTTPRequestHandler):
    # set via make_http_server on the server instance
    server: "PolicySyncHTTPServer"

    def _respond(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        if self.path == "/healthz":
            self._respond(200, {"status": "ok", **self.server.state.snapshot()})
        else:
            self._respond(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/hooks/policy":
            self._respond(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length <= 0 or length > MAX_BODY:
            self._respond(400, {"error": "missing or oversized body"})
            return
        body = self.rfile.read(length)

        signature = (self.headers.get("X-Gitea-Signature") or "").strip().lower()
        expected = hmac.new(self.server.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        if not signature or not hmac.compare_digest(signature, expected):
            log.warning("rejected webhook with bad signature from %s", self.client_address[0])
            self._respond(403, {"error": "invalid signature"})
            return

        event = self.headers.get("X-Gitea-Event", "")
        if event != "push":
            self._respond(200, {"status": "ignored", "event": event})
            return

        self.server.trigger_sync()
        self._respond(202, {"status": "sync scheduled"})

    def log_message(self, fmt: str, *args) -> None:
        log.debug("%s %s", self.client_address[0], fmt % args)


class PolicySyncHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, webhook_secret: str, trigger_sync, state: SyncState):
        super().__init__(addr, PolicySyncHandler)
        self.webhook_secret = webhook_secret
        self.trigger_sync = trigger_sync
        self.state = state


def make_http_server(host: str, port: int, webhook_secret: str, trigger_sync, state: SyncState) -> PolicySyncHTTPServer:
    return PolicySyncHTTPServer((host, port), webhook_secret, trigger_sync, state)


def run_sync_worker(syncer: Syncer, state: SyncState, wake: threading.Event, poll_interval: float, stop: threading.Event) -> None:
    """Startup sync, then re-sync on webhook wake-ups or every poll_interval."""
    while not stop.is_set():
        try:
            ok = syncer.sync_with_retry()
        except Exception:
            log.exception("sync worker caught unexpected error")  # never die
            ok = False
        state.record(ok)
        wake.wait(poll_interval)
        wake.clear()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = Config.from_env()
    state = SyncState()
    wake = threading.Event()
    stop = threading.Event()
    syncer = Syncer(cfg)

    worker = threading.Thread(
        target=run_sync_worker,
        args=(syncer, state, wake, cfg.poll_interval, stop),
        name="sync-worker",
        daemon=True,
    )
    worker.start()

    httpd = make_http_server("0.0.0.0", cfg.port, cfg.webhook_secret, wake.set, state)
    log.info("policy-sync listening on :%d (gitea=%s devpi=%s poll=%.0fs)",
             cfg.port, cfg.gitea_url, cfg.devpi_url, cfg.poll_interval)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        wake.set()
        httpd.server_close()
