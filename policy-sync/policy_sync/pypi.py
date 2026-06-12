"""PyPI public-cache proxy helpers.

The gateway sends public PyPI fallback traffic here so Artea can enforce policy
that devpi-constrained cannot express, currently a minimum upstream age. Version
constraints still live in devpi-constrained; this layer filters the constrained
simple page further and guards file URLs that would otherwise bypass metadata.
"""

from __future__ import annotations

import html
import json
import logging
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import TYPE_CHECKING

from .config import Config
from .store import PolicyStore

if TYPE_CHECKING:
    from .server import PolicySyncHandler

log = logging.getLogger(__name__)

AGE_DIRECTIVE_RE = re.compile(
    r"^\s*#\s*artea:\s*(?:min(?:imum)?[-_]upstream[-_]age)\s*=\s*([^\s#]+)",
    re.IGNORECASE,
)
DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)\s*$", re.IGNORECASE)
DURATION_UNITS = {
    "ms": 0.001,
    "s": 1.0,
    "m": 60.0,
    "h": 60.0 * 60.0,
    "d": 24.0 * 60.0 * 60.0,
}
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


class PypiPolicyError(Exception):
    pass


class PypiUpstreamError(Exception):
    pass


@dataclass(frozen=True)
class PypiPolicy:
    min_age_seconds: float = 0.0


@dataclass
class ProjectMetadata:
    fetched_at: float
    files: dict[str, float]


def parse_duration_seconds(raw: str) -> float:
    match = DURATION_RE.match(raw)
    if not match:
        raise PypiPolicyError("duration must use units ms, s, m, h, or d")
    seconds = float(match.group(1)) * DURATION_UNITS[match.group(2).lower()]
    if seconds < 0:
        raise PypiPolicyError("duration must be non-negative")
    return seconds


def parse_pypi_policy(text: str) -> PypiPolicy:
    min_age = 0.0
    for line in text.splitlines():
        match = AGE_DIRECTIVE_RE.match(line)
        if match:
            min_age = parse_duration_seconds(match.group(1))
    return PypiPolicy(min_age_seconds=min_age)


def _iso_to_epoch(raw: str) -> float | None:
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _json_request(url: str, timeout: float = 10.0) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        raise PypiUpstreamError(f"GET {url} -> HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise PypiUpstreamError(f"GET {url} failed: {e}") from e


def _filename_from_path(path: str) -> str:
    return urllib.parse.unquote(path.rstrip("/").rsplit("/", 1)[-1])


def _same_origin_url(handler: "PolicySyncHandler", path: str, query: str = "", fragment: str = "") -> str:
    proto = handler.headers.get("X-Forwarded-Proto") or "http"
    host = handler.headers.get("X-Forwarded-Host") or handler.headers.get("Host") or ""
    url = f"{proto}://{host}{path}" if host else path
    if query:
        url += f"?{query}"
    if fragment:
        url += f"#{fragment}"
    return url


class SimplePageFilter(HTMLParser):
    def __init__(self, proxy: "PypiProxy", handler: "PolicySyncHandler", project: str, policy: PypiPolicy):
        super().__init__(convert_charrefs=False)
        self.proxy = proxy
        self.handler = handler
        self.project = project
        self.policy = policy
        self.out: list[str] = []
        self.skip_anchor = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.skip_anchor:
            return
        if tag.lower() == "a":
            href = next((v for k, v in attrs if k.lower() == "href"), None)
            if href is not None:
                keep, rewritten = self.proxy.rewrite_simple_href(self.handler, self.project, href, self.policy)
                if not keep:
                    self.skip_anchor = True
                    return
                attrs = [(k, rewritten if k.lower() == "href" else v) for k, v in attrs]
        self.out.append(self._start(tag, attrs))

    def handle_endtag(self, tag: str) -> None:
        if self.skip_anchor:
            if tag.lower() == "a":
                self.skip_anchor = False
            return
        self.out.append(f"</{tag}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if not self.skip_anchor:
            self.out.append(self._start(tag, attrs, close=True))

    def handle_data(self, data: str) -> None:
        if not self.skip_anchor:
            self.out.append(data)

    def handle_entityref(self, name: str) -> None:
        if not self.skip_anchor:
            self.out.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if not self.skip_anchor:
            self.out.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        if not self.skip_anchor:
            self.out.append(f"<!--{data}-->")

    def handle_decl(self, decl: str) -> None:
        self.out.append(f"<!{decl}>")

    def _start(self, tag: str, attrs: list[tuple[str, str | None]], close: bool = False) -> str:
        rendered = [f"<{tag}"]
        for key, value in attrs:
            rendered.append(f" {key}" if value is None else f' {key}="{html.escape(value, quote=True)}"')
        rendered.append(" />" if close else ">")
        return "".join(rendered)


class PypiProxy:
    def __init__(self, cfg: Config, policy_store: PolicyStore, now=time.time):
        self.cfg = cfg
        self.policy_store = policy_store
        self.now = now
        self.metadata_cache: dict[str, ProjectMetadata] = {}
        self.file_project_cache: dict[str, str] = {}

    def matches(self, path: str) -> bool:
        parsed = urllib.parse.urlsplit(path)
        return parsed.path.startswith("/pypi/simple/") or parsed.path.startswith("/pypi/files/") or parsed.path.startswith("/root/")

    def serve(self, handler: "PolicySyncHandler", head: bool = False) -> None:
        parsed = urllib.parse.urlsplit(handler.path)
        path = parsed.path
        if path.startswith("/pypi/simple/"):
            project = path.removeprefix("/pypi/simple/").strip("/")
            self.serve_simple(handler, urllib.parse.unquote(project), head=head)
            return
        if path.startswith("/pypi/files/"):
            self.serve_guarded_file(handler, path.removeprefix("/pypi/files/"), parsed.query, head=head)
            return
        if path.startswith("/root/"):
            self.serve_root(handler, path, parsed.query, head=head)
            return
        handler._respond(404, {"error": "not found"})

    def policy(self) -> PypiPolicy:
        got = self.policy_store.get()
        if got is None:
            raise PypiPolicyError("no pypi policy has been synced yet")
        content, _ = got
        return parse_pypi_policy(content.decode("utf-8", errors="replace"))

    def serve_simple(self, handler: "PolicySyncHandler", project: str, head: bool = False) -> None:
        try:
            policy = self.policy()
            devpi_path = f"/root/constrained/+simple/{urllib.parse.quote(project)}/" if project else "/root/constrained/+simple/"
            status, headers, body = self.fetch_devpi(devpi_path)
            if status != 200 or not project:
                self.send(handler, status, headers, body, head=head)
                return
            filtered = self.filter_simple_body(handler, project, body, policy)
            headers = [(k, v) for k, v in headers if k.lower() != "content-length"]
            headers = [(k, v) for k, v in headers if k.lower() != "content-type"]
            headers.append(("Content-Type", "text/html; charset=utf-8"))
            self.send(handler, 200, headers, filtered, head=head)
        except PypiPolicyError as e:
            handler._respond(503, {"error": f"pypi policy unavailable: {e}; registry is failing closed"})
        except PypiUpstreamError as e:
            handler._respond(503, {"error": f"pypi upstream unavailable: {e}; registry is failing closed"})

    def serve_guarded_file(self, handler: "PolicySyncHandler", rest: str, query: str, head: bool = False) -> None:
        project, sep, original = rest.partition("/")
        if not sep or not original:
            handler._respond(400, {"error": "guarded pypi file URL must include project and devpi path"})
            return
        project = urllib.parse.unquote(project)
        path = "/" + original
        self.serve_checked_file(handler, project, path, query, head=head)

    def serve_root(self, handler: "PolicySyncHandler", path: str, query: str, head: bool = False) -> None:
        decoded = urllib.parse.unquote(path)
        if decoded.startswith("/root/constrained/+simple/"):
            project = decoded.removeprefix("/root/constrained/+simple/").strip("/")
            self.serve_simple(handler, project, head=head)
            return
        try:
            policy = self.policy()
        except PypiPolicyError as e:
            handler._respond(503, {"error": f"pypi policy unavailable: {e}; registry is failing closed"})
            return
        if policy.min_age_seconds <= 0:
            self.proxy_devpi(handler, path, query, head=head)
            return
        project = self.file_project_cache.get(path)
        if project is None:
            handler._respond(403, {"error": "forbidden: public pypi file requires guarded project URL for upstream age verification"})
            return
        self.serve_checked_file(handler, project, path, query, policy=policy, head=head)

    def serve_checked_file(
        self,
        handler: "PolicySyncHandler",
        project: str,
        path: str,
        query: str,
        policy: PypiPolicy | None = None,
        head: bool = False,
    ) -> None:
        try:
            policy = policy or self.policy()
            if policy.min_age_seconds > 0 and not self.file_allowed(project, path, policy):
                handler._respond(403, {"error": f"forbidden: {_filename_from_path(path)} is newer than the registry minimum upstream age"})
                return
            self.proxy_devpi(handler, path, query, head=head)
        except PypiPolicyError as e:
            handler._respond(503, {"error": f"pypi policy unavailable: {e}; registry is failing closed"})
        except PypiUpstreamError as e:
            handler._respond(503, {"error": f"pypi upstream unavailable: {e}; registry is failing closed"})

    def filter_simple_body(self, handler: "PolicySyncHandler", project: str, body: bytes, policy: PypiPolicy) -> bytes:
        if policy.min_age_seconds > 0:
            # Prime metadata once. If this fails, every link is hidden fail-closed.
            try:
                self.project_metadata(project)
            except PypiUpstreamError as e:
                log.warning("pypi metadata lookup for %s failed: %s", project, e)
        parser = SimplePageFilter(self, handler, project, policy)
        parser.feed(body.decode("utf-8", errors="replace"))
        parser.close()
        return "".join(parser.out).encode()

    def rewrite_simple_href(
        self,
        handler: "PolicySyncHandler",
        project: str,
        href: str,
        policy: PypiPolicy,
    ) -> tuple[bool, str]:
        split = urllib.parse.urlsplit(href)
        path = split.path
        if not path.startswith("/"):
            base = f"/root/constrained/+simple/{urllib.parse.quote(project)}/"
            path = urllib.parse.urlsplit(urllib.parse.urljoin(base, href)).path
        if policy.min_age_seconds > 0 and not self.file_allowed(project, path, policy):
            return False, href
        self.file_project_cache[path] = project
        guarded = f"/pypi/files/{urllib.parse.quote(project, safe='')}{path}"
        return True, _same_origin_url(handler, guarded, split.query, split.fragment)

    def file_allowed(self, project: str, path: str, policy: PypiPolicy) -> bool:
        if policy.min_age_seconds <= 0:
            return True
        filename = _filename_from_path(path)
        metadata = self.project_metadata(project)
        uploaded = metadata.files.get(filename)
        if uploaded is None:
            return False
        return self.now() - uploaded >= policy.min_age_seconds

    def project_metadata(self, project: str) -> ProjectMetadata:
        cached = self.metadata_cache.get(project)
        if cached is not None and self.now() - cached.fetched_at < self.cfg.pypi_metadata_cache_seconds:
            return cached
        url = f"{self.cfg.pypi_json_url}/{urllib.parse.quote(project)}/json"
        data = _json_request(url)
        files: dict[str, float] = {}
        releases = data.get("releases")
        if isinstance(releases, dict):
            for release_files in releases.values():
                if not isinstance(release_files, list):
                    continue
                for item in release_files:
                    if not isinstance(item, dict):
                        continue
                    filename = item.get("filename")
                    uploaded_raw = item.get("upload_time_iso_8601") or item.get("upload_time")
                    if isinstance(filename, str) and isinstance(uploaded_raw, str):
                        uploaded = _iso_to_epoch(uploaded_raw)
                        if uploaded is not None:
                            files[filename] = uploaded
        metadata = ProjectMetadata(fetched_at=self.now(), files=files)
        self.metadata_cache[project] = metadata
        return metadata

    def fetch_devpi(self, path: str) -> tuple[int, list[tuple[str, str]], bytes]:
        url = f"{self.cfg.devpi_url}{path}"
        req = urllib.request.Request(url, headers={"Accept": "text/html,application/octet-stream"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, list(resp.headers.items()), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, list(e.headers.items()), e.read()
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise PypiUpstreamError(f"GET {url} failed: {e}") from e

    def proxy_devpi(self, handler: "PolicySyncHandler", path: str, query: str, head: bool = False) -> None:
        target = path + (f"?{query}" if query else "")
        url = f"{self.cfg.devpi_url}{target}"
        req = urllib.request.Request(url, method="HEAD" if head else "GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                handler.send_response(resp.status)
                for key, value in resp.headers.items():
                    if key.lower() not in HOP_BY_HOP:
                        handler.send_header(key, value)
                handler.end_headers()
                if not head:
                    shutil.copyfileobj(resp, handler.wfile)
        except urllib.error.HTTPError as e:
            handler.send_response(e.code)
            for key, value in e.headers.items():
                if key.lower() not in HOP_BY_HOP:
                    handler.send_header(key, value)
            handler.end_headers()
            if not head:
                handler.wfile.write(e.read())
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise PypiUpstreamError(f"GET {url} failed: {e}") from e

    def send(
        self,
        handler: "PolicySyncHandler",
        status: int,
        headers: list[tuple[str, str]],
        body: bytes,
        head: bool = False,
    ) -> None:
        handler.send_response(status)
        for key, value in headers:
            if key.lower() not in HOP_BY_HOP and key.lower() != "content-length":
                handler.send_header(key, value)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        if not head:
            handler.wfile.write(body)
