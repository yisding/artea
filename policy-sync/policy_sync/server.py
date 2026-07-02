"""HTTP surface: Gitea push-webhook receiver, health endpoint, and the npm
policy endpoint (`GET /policy/npm-rules.yaml`) that the Verdaccio filter plugin
polls in K8s deployments (no shared volume there).

Stdlib http.server is enough here: a few routes, internal-only traffic, and the
actual sync work runs on a single background worker thread (webhooks only set
an event, so deliveries return immediately and concurrent syncs are coalesced).
The policy endpoint is unauthenticated by design: it is cluster-internal and
serves block rules, not secrets.
"""

import hmac
import hashlib
import json
import logging
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import enrich
from . import osv
from .config import Config
from .policy_model import PolicyError
from .store import ParsedPolicyStore, PolicyStore
from .sync import Syncer

log = logging.getLogger(__name__)

MAX_BODY = 10 * 1024 * 1024  # webhook payloads are small; cap reads defensively
LISTEN_PORT = 8920

POLICY_ENDPOINT = "/policy/npm-rules.yaml"
UPSTREAM_POLICY_ENDPOINT = "/policy/upstream-policy.yaml"
# PEP 700 upload-time enrichment for the PyPI Simple API (the gateway njs
# orchestrator calls this after it has decided Gitea-first vs devpi fallback).
ENRICH_ENDPOINT = "/pypi/simple-enrich"
OSV_ENDPOINT = "/osv/querybatch"
SLOW_OSV_DECISION_SECONDS = 0.5

# The name is already PEP 503-normalized by the gateway; reject anything that
# is not (defense in depth — never interpolate an arbitrary string into an
# upstream URL).
_NORMALIZED_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*$")


def etag_matches(if_none_match: str, etag: str) -> bool:
    """If-None-Match comparison (RFC 9110: weak comparison, list or `*`)."""
    candidates = [c.strip() for c in if_none_match.split(",")]
    return "*" in candidates or etag in (c.removeprefix("W/") for c in candidates)


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
    # Narrow the inherited `server` attribute to our subclass so cfg/state/store
    # resolve. (Narrowing an inherited mutable attribute is intentional here.)
    server: "PolicySyncHTTPServer"  # pyright: ignore[reportIncompatibleVariableOverride]

    def _respond(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _respond_raw(
        self, code: int, body: bytes, content_type: str, extra_headers: dict[str, str] | None = None
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        for header, value in (extra_headers or {}).items():
            self.send_header(header, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        if self.path == "/healthz":
            self._respond(200, {"status": "ok", **self.server.state.snapshot()})
        elif self.path == POLICY_ENDPOINT:
            self._serve_policy(self.server.store, "npm")
        elif self.path == UPSTREAM_POLICY_ENDPOINT:
            self._serve_policy(self.server.upstream_store, "upstream")
        elif urlsplit(self.path).path == ENRICH_ENDPOINT:
            self._serve_enrichment()
        else:
            self._respond(404, {"error": "not found"})

    def _read_json_body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length <= 0 or length > MAX_BODY:
            self._respond(400, {"error": "missing or oversized body"})
            return None
        try:
            payload = json.loads(self.rfile.read(length))
        except ValueError:
            self._respond(400, {"error": "request body must be JSON"})
            return None
        if not isinstance(payload, dict):
            self._respond(400, {"error": "request body must be a JSON object"})
            return None
        return payload

    def _serve_enrichment(self) -> None:
        """GET /pypi/simple-enrich?upstream={devpi|gitea}&name=<normalized>.

        Returns a PEP 700 v1.1 Simple API JSON document for the project, sourced
        from the winning upstream the gateway already selected. The client's
        Authorization header is forwarded to Gitea (never logged) so Gitea
        re-enforces package read permissions on the private path.
        """
        query = parse_qs(urlsplit(self.path).query)
        upstream = (query.get("upstream") or [""])[0]
        name = (query.get("name") or [""])[0]
        if upstream not in ("devpi", "gitea") or not _NORMALIZED_NAME_RE.match(name):
            self._respond(400, {"error": "upstream must be devpi|gitea and name PEP 503-normalized"})
            return

        cfg = self.server.cfg
        if cfg is None:
            # Enrichment needs gitea_url/devpi_url/namespace; if the server was
            # constructed without a Config (e.g. a minimal test), this endpoint is
            # simply unavailable rather than crashing the worker.
            self._respond(503, {"error": "enrichment not configured"})
            return
        authorization = self.headers.get("Authorization") or ""
        try:
            if upstream == "gitea":
                doc = enrich.enrich_gitea(name, cfg.gitea_url, cfg.namespace, authorization)
                if doc is None:
                    # No such private package -> drive the gateway fall-through
                    # to the public mirror (preserves Gitea-first precedence).
                    self._respond(404, {"error": "no such private package"})
                    return
            else:
                # Public path now sources per-file metadata from devpi's intra-
                # cluster /+artea/project-meta endpoint (cfg.pypi_json_url is no
                # longer consulted here — see enrich._fetch_devpi_file_meta).
                doc = enrich.enrich_devpi(name, cfg.devpi_url)
        except enrich.EnrichNotFound:
            # The public mirror has no such project — a real 404 ("no
            # candidates"), not a transient 502. Mirrors the uncached HTML
            # fallback's 404 so JSON pip/uv stop here instead of retrying.
            self._respond(404, {"error": "no such project"})
            return
        except enrich.EnrichUnavailable as e:
            log.warning("enrichment unavailable (upstream=%s name=%s): %s", upstream, name, e)
            self._respond(502, {"error": "upstream metadata unavailable"})
            return
        except Exception:  # never crash the worker on a malformed upstream reply
            log.exception("enrichment failed (upstream=%s name=%s)", upstream, name)
            self._respond(502, {"error": "enrichment failed"})
            return

        body = json.dumps(doc).encode()
        self._respond_raw(200, body, enrich.SIMPLE_JSON_ACCEPT)

    def do_HEAD(self) -> None:  # noqa: N802
        # The response writers (_respond / _respond_raw) both suppress the body
        # when self.command == "HEAD", so a HEAD is just a GET whose body is
        # dropped — route it through do_GET rather than duplicate the path table.
        self.do_GET()

    def _serve_policy(self, store: PolicyStore, label: str) -> None:
        got = store.get()
        if got is None:
            self._respond(404, {"error": f"no {label} policy has been synced yet; check Gitea connectivity and /healthz"})
            return
        content, etag = got
        if_none_match = self.headers.get("If-None-Match")
        if if_none_match and etag_matches(if_none_match, etag):
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return
        self._respond_raw(200, content, "application/yaml", {"ETag": etag})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == OSV_ENDPOINT:
            self._serve_osv_querybatch()
            return
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

    def _serve_osv_querybatch(self) -> None:
        started = time.perf_counter()
        payload = self._read_json_body()
        if payload is None:
            return
        ecosystem = payload.get("ecosystem")
        name = payload.get("name")
        versions = payload.get("versions")
        package_summary = payload.get("package_summary") is True
        blocked_only = payload.get("blocked_only") is True
        if not isinstance(ecosystem, str) or not isinstance(name, str):
            self._respond(400, {"error": "ecosystem and name are required"})
            return
        if not package_summary and not isinstance(versions, list):
            self._respond(400, {"error": "versions are required"})
            return
        if isinstance(versions, list) and len(versions) > 10000:
            self._respond(400, {"error": "too many versions"})
            return
        try:
            if package_summary:
                mode = "package_summary"
                result = self.server.osv_client.summarize_package(
                    self.server.parsed_policy_store.get(),
                    ecosystem,
                    name,
                )
            else:
                mode = "versioned"
                result = self.server.osv_client.decide(
                    self.server.parsed_policy_store.get(),
                    ecosystem,
                    name,
                    versions,
                )
        except PolicyError as e:
            self._respond(400, {"error": str(e)})
            return
        except Exception:  # never crash the worker on an unexpected decision error
            log.exception("OSV querybatch decision failed (name=%s)", name)
            self._respond(500, {"error": "OSV decision failed"})
            return
        elapsed = time.perf_counter() - started
        if result.status != "ok" or elapsed >= SLOW_OSV_DECISION_SECONDS:
            version_count = len(versions) if isinstance(versions, list) else 0
            log.info(
                "OSV querybatch mode=%s ecosystem=%s name=%s versions=%s status=%s verdicts=%s elapsed_ms=%.1f",
                mode,
                ecosystem,
                name,
                version_count,
                result.status,
                len(result.verdicts),
                elapsed * 1000,
            )
        self._respond(200, osv.response_payload(result, blocked_only=blocked_only or package_summary))

    def log_message(self, format: str, *args) -> None:  # noqa: A002 (match BaseHTTPRequestHandler signature)
        log.debug("%s %s", self.client_address[0], format % args)


class PolicySyncHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        addr,
        webhook_secret: str,
        trigger_sync,
        state: SyncState,
        store: PolicyStore,
        upstream_store: PolicyStore,
        parsed_policy_store: ParsedPolicyStore | None = None,
        osv_client: osv.OsvClient | None = None,
        cfg: Config | None = None,
    ):
        super().__init__(addr, PolicySyncHandler)
        self.webhook_secret = webhook_secret
        self.trigger_sync = trigger_sync
        self.state = state
        self.store = store
        self.upstream_store = upstream_store
        self.parsed_policy_store = parsed_policy_store or ParsedPolicyStore()
        self.osv_client = osv_client or osv.OsvClient()
        # Enrichment endpoint reads cfg.gitea_url / cfg.devpi_url / cfg.namespace;
        # None leaves that endpoint disabled (503).
        self.cfg = cfg


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
    # the file fallback keeps the endpoint serving across restarts in file mode
    # (test/local inspection), where the volume still holds the last synced policy
    store = PolicyStore(fallback_path=cfg.policy_file_path)
    upstream_store = PolicyStore(fallback_path=cfg.upstream_policy_file_path)
    parsed_policy_store = ParsedPolicyStore(fallback_path=cfg.parsed_policy_file_path)
    osv_client = osv.OsvClient(
        api_url=cfg.osv_api_url,
        timeout=cfg.osv_timeout_seconds,
        positive_ttl=cfg.osv_positive_ttl_seconds,
        negative_ttl=cfg.osv_negative_ttl_seconds,
        batch_size=cfg.osv_batch_size,
        max_concurrency=cfg.osv_max_concurrency,
        cache_file_path=cfg.osv_cache_file_path,
    )
    syncer = Syncer(cfg, store=store, upstream_store=upstream_store, parsed_policy_store=parsed_policy_store)

    worker = threading.Thread(
        target=run_sync_worker,
        args=(syncer, state, wake, cfg.poll_interval, stop),
        name="sync-worker",
        daemon=True,
    )
    worker.start()

    httpd = PolicySyncHTTPServer(
        ("0.0.0.0", LISTEN_PORT),
        cfg.webhook_secret,
        wake.set,
        state,
        store,
        upstream_store,
        parsed_policy_store,
        osv_client,
        cfg,
    )
    log.info("policy-sync listening on :%d (gitea=%s devpi=%s poll=%.0fs file=%s)",
             LISTEN_PORT, cfg.gitea_url, cfg.devpi_url, cfg.poll_interval,
             cfg.policy_file_path or "<disabled: HTTP-only>")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        wake.set()
        httpd.server_close()
