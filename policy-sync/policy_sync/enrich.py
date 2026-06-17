"""PEP 700 upload-time enrichment for the Artea PyPI Simple API.

The gateway serves a single PyPI Simple index (`GET /pypi/simple/<name>/`) that
routes Gitea-first with a devpi (public pull-through) 404 fallback. Neither
upstream emits PEP 700 `upload-time` in its JSON Simple API:

  * devpi (public) returns PEP 691 JSON at `meta.api-version` "1.0" with only
    filename/url/hashes per file — no upload-time;
  * Gitea (private) ignores the JSON Accept header entirely and only serves
    PEP 503 HTML.

This module is the Artea-owned enrichment layer (R7: stock upstream images, the
gateway/policy-sync/config are ours). For a `application/vnd.pypi.simple.v1+json`
request the gateway njs orchestrator (gateway/njs/pep700.js) probes Gitea-first
and then calls the policy-sync endpoint here with the winning upstream. We fetch
the base PEP 691 list ourselves, join it with the timestamps each upstream
exposes through a documented stock API, and return a PEP 700 v1.1 document:

  * `meta.api-version` becomes "1.1";
  * each `files[]` entry gains `upload-time` (canonical UTC ISO-8601 with
    microsecond precision and a `Z` suffix) and `size` — PEP 700 makes `size`
    mandatory in 1.1, so it is sourced for BOTH paths (PyPI JSON for public;
    Gitea's per-version package-files API for private). A file may still lack
    `size`/`upload-time` if the upstream omits it for that exact filename;
    pip/uv tolerate that, and a strict consumer simply skips the un-annotated
    file (the safe direction);
  * a top-level `versions[]` array is added.

Composition with the server-side age gate: the PUBLIC base list is taken from
devpi's `root/constrained/+simple/<name>/` (POST policy — the ConstrainedStage
already dropped files newer than `min_upstream_age` and outside constraints), so
enrichment only ever annotates files the gate already permits. It never
re-introduces a filtered file.

Availability vs. metadata: the base index list is what makes a package
installable; `upload-time`/`size` are optional annotations. A reachable base
list is therefore served even when the optional metadata source is momentarily
down (un-annotated, not 502), so a metadata blip never breaks a plain install;
only an unreachable base list (or a Gitea outage, handled in the gateway) is an
error. See enrich_devpi/enrich_gitea for the precise fail-open/closed split.

Stdlib only (urllib + json), matching the rest of policy-sync.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser

log = logging.getLogger(__name__)

API_VERSION = "1.1"
SIMPLE_JSON_ACCEPT = "application/vnd.pypi.simple.v1+json"

# Enriched-document cache (keyed by upstream+name). Short TTL: collapses pip's
# repeated same-session index hits without serving wildly stale lists.
ENRICH_TTL_SECONDS = 300.0
# Gitea per-version created_at is immutable once published; cache it for longer
# so the private per-version fan-out is paid at most once per version per hour.
CREATED_AT_TTL_SECONDS = 3600.0
# How long a stale enriched public document may be served when the timestamp
# source is unreachable (fail-closed-but-bounded; private has no safe stale path).
STALE_MAX_SECONDS = 24 * 60 * 60

_HTTP_TIMEOUT = 10


class EnrichError(Exception):
    """Base class for enrichment failures."""


class EnrichUnavailable(EnrichError):
    """Timestamp metadata could not be obtained and no usable cache exists."""


class EnrichNotFound(EnrichError):
    """The upstream mirror has no such project — a real 404, not an outage.

    Kept distinct from EnrichUnavailable so the server returns 404 ("no
    candidates") rather than 502 ("transient"): the uncached HTML fallback
    returns 404 here, and JSON pip/uv must read 404 the same way.
    """


class _TTLCache:
    """Tiny thread-safe time-keyed cache (dict + wall clock).

    Bounded: `put` opportunistically sweeps entries older than `max_age` (the
    longest window any reader cares about — the TTL for plain caches, or the
    stale-serve window for caches also read via `get_stale`) and, if still over
    `max_entries`, evicts the oldest by insertion time. This keeps a long-lived
    mirror from growing one entry per package name forever.
    """

    def __init__(self, ttl: float, now=time.time, max_entries: int = 4096, max_age: float | None = None):
        self._ttl = ttl
        self._now = now
        self._max_entries = max_entries
        self._max_age = ttl if max_age is None else max_age
        self._lock = threading.Lock()
        self._data: dict = {}

    def get(self, key):
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            stored_at, value = entry
            if self._now() - stored_at >= self._ttl:
                return None
            return value

    def get_stale(self, key, max_age: float):
        """Return a value past its TTL but younger than max_age (else None)."""
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            stored_at, value = entry
            if self._now() - stored_at >= max_age:
                return None
            return value

    def put(self, key, value):
        with self._lock:
            now = self._now()
            self._data[key] = (now, value)
            if len(self._data) > self._max_entries:
                # Drop entries past the longest retention window first ...
                for k in [k for k, (ts, _v) in list(self._data.items()) if now - ts >= self._max_age]:
                    del self._data[k]
                # ... then, if still over capacity, evict oldest by insertion time.
                overflow = len(self._data) - self._max_entries
                if overflow > 0:
                    for k, _entry in sorted(self._data.items(), key=lambda kv: kv[1][0])[:overflow]:
                        del self._data[k]


# get_stale serves an enriched doc up to STALE_MAX_SECONDS old, so eviction must
# retain entries for that window (max_age), not merely the short TTL.
_enrich_cache = _TTLCache(ENRICH_TTL_SECONDS, max_age=STALE_MAX_SECONDS)
_created_at_cache = _TTLCache(CREATED_AT_TTL_SECONDS)


# ---- timestamp helpers ----------------------------------------------------------

def iso_to_epoch(raw: str) -> float | None:
    """Parse an ISO-8601 timestamp to epoch seconds (copied from the devpi
    plugin's iso_to_epoch so policy-sync need not import across the image
    boundary). Returns None on anything unparseable."""
    if not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def epoch_to_iso_z(epoch: float) -> str:
    """Canonical PEP 700 upload-time: UTC, microsecond precision, Z suffix."""
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    # strftime drops microseconds when zero; build them explicitly so the format
    # is always YYYY-MM-DDTHH:MM:SS.ffffffZ (pip parses fractional seconds).
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond:06d}Z"


def normalize_to_iso_z(raw: str) -> str | None:
    """Normalize any ISO-8601 timestamp to the canonical microsecond-Z form.

    Gitea's created_at is second-precision (e.g. 2026-06-16T02:21:17Z); this
    pads it to .000000Z so the served value always matches the spec shape.
    """
    epoch = iso_to_epoch(raw)
    if epoch is None:
        return None
    return epoch_to_iso_z(epoch)


_VERSION_SPLIT_RE = re.compile(r"\.(?:tar\.gz|tar\.bz2|tgz|zip|whl|egg)$", re.IGNORECASE)


def version_from_filename(filename: str, name: str) -> str | None:
    """Best-effort sdist/wheel version extraction for the top-level versions[].

    Wheels: <distribution>-<version>(-<build>)?-<pytag>-<abitag>-<platform>.whl
    Sdists: <name>-<version>.tar.gz / .zip / ...
    Returns None when the version cannot be confidently extracted.
    """
    if not filename:
        return None
    stem = _VERSION_SPLIT_RE.sub("", filename)
    low = filename.lower()
    if low.endswith(".whl") or low.endswith(".egg"):
        parts = stem.split("-")
        if len(parts) >= 2:
            return parts[1]
        return None
    # sdist: strip the leading "<name>-" prefix (names normalize - _ . loosely)
    prefix = stem[: len(name)]
    rest = stem[len(name):]
    if rest.startswith("-") and _loose_eq(prefix, name):
        return rest[1:]
    # fall back to last hyphen group
    if "-" in stem:
        return stem.rsplit("-", 1)[1]
    return None


def _loose_eq(a: str, b: str) -> bool:
    def norm(s: str) -> str:
        return re.sub(r"[-_.]+", "-", s).lower()
    return norm(a) == norm(b)


# ---- HTTP helpers ---------------------------------------------------------------

def _get(url: str, headers: dict[str, str], timeout: int = _HTTP_TIMEOUT) -> bytes:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# ---- v1.1 document builder ------------------------------------------------------

def build_v1_1(name: str, files: list[dict], versions: list[str]) -> dict:
    """Assemble the PEP 700 v1.1 Simple API document.

    `files` entries are already in their final shape (filename/url/hashes plus
    any upload-time/size/requires-python/yanked we could derive). This only
    fixes the envelope: meta.api-version "1.1", name, files, and a sorted-unique
    top-level versions[].
    """
    return {
        "meta": {"api-version": API_VERSION},
        "name": name,
        "files": files,
        "versions": sorted(set(versions)),
    }


# ---- public (devpi) -------------------------------------------------------------

def _fetch_pypi_file_meta(name: str, pypi_json_url: str) -> dict[str, dict]:
    """Map exact filename -> {upload-time, size, requires-python, yanked} from
    the PyPI JSON API. This is the same source the devpi policy plugin parses
    for the age gate, so the value we surface matches the value the gate used."""
    url = f"{pypi_json_url.rstrip('/')}/{urllib.parse.quote(name)}/json"
    try:
        raw = _get(url, {"Accept": "application/json"})
        data = json.loads(raw)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise EnrichUnavailable(f"pypi json {url}: {e}") from e

    out: dict[str, dict] = {}
    releases = data.get("releases")
    if not isinstance(releases, dict):
        return out
    for _version, release_files in releases.items():
        if not isinstance(release_files, list):
            continue
        for item in release_files:
            if not isinstance(item, dict):
                continue
            filename = item.get("filename")
            if not isinstance(filename, str):
                continue
            entry: dict = {}
            uploaded_raw = item.get("upload_time_iso_8601") or item.get("upload_time")
            iso = normalize_to_iso_z(uploaded_raw) if isinstance(uploaded_raw, str) else None
            if iso is not None:
                entry["upload-time"] = iso
            size = item.get("size")
            if isinstance(size, int) and size >= 0:
                entry["size"] = size
            requires_python = item.get("requires_python")
            if isinstance(requires_python, str) and requires_python:
                entry["requires-python"] = requires_python
            yanked = item.get("yanked")
            if yanked:  # PyPI sends false/None for not-yanked; only surface truthy
                entry["yanked"] = yanked if isinstance(yanked, str) else True
            out[filename] = entry
    return out


def enrich_devpi(name: str, devpi_url: str, pypi_json_url: str) -> dict:
    """Build a v1.1 document for a PUBLIC package from devpi's constrained index.

    The base file list is devpi's POST-policy PEP 691 JSON, so only permitted
    files are annotated. upload-time/size come from the PyPI JSON API (the
    authoritative source the age gate already consulted). Files with no metadata
    match keep no upload-time (it is spec-optional); a time-filtering client
    simply will not select them, which is the safe direction.

    Availability vs. metadata are decoupled. The devpi constrained list is the
    *index* (which files exist); the PyPI JSON is *optional metadata*
    (upload-time/size). If the base list is unreachable and no stale enriched
    document exists, we fail closed (EnrichUnavailable -> 502): a synthesized
    empty list would look like "no such package". But if the base list is
    reachable and only the PyPI JSON is down, we serve the base list as-is
    (still a complete, installable v1.1 index) rather than 502 — upload-time is
    spec-optional, and a time-filtering client that needs it simply will not
    match the un-stamped files, the same safe direction the per-file-miss path
    already relies on. We do NOT cache a metadata-degraded document, so the next
    request retries PyPI JSON instead of pinning the degraded list for the TTL.
    """
    cache_key = ("devpi", name)
    base_url = f"{devpi_url.rstrip('/')}/root/constrained/+simple/{urllib.parse.quote(name)}/"
    try:
        raw = _get(base_url, {"Accept": SIMPLE_JSON_ACCEPT})
        base = json.loads(raw)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # Absent from the public mirror too — the constrained index 404s
            # exactly as the HTML fallback does. This is a real "no such
            # project", not an outage: do NOT serve stale and do NOT 502, so the
            # client gets 404 ("no candidates") instead of treating a missing
            # package as a transient index failure and retrying.
            raise EnrichNotFound(f"devpi simple {base_url}: 404") from e
        # Other HTTP errors (5xx, etc.): try last-good, else fail closed.
        stale = _enrich_cache.get_stale(cache_key, STALE_MAX_SECONDS)
        if stale is not None:
            log.warning("devpi simple base unavailable for %s (HTTP %s); serving stale enriched", name, e.code)
            return stale
        raise EnrichUnavailable(f"devpi simple {base_url}: HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        # Base list unreachable/garbled: try last-good, else fail closed. We do
        # NOT synthesize an empty list (that would look like "no such package").
        stale = _enrich_cache.get_stale(cache_key, STALE_MAX_SECONDS)
        if stale is not None:
            log.warning("devpi simple base unavailable for %s (%s); serving stale enriched", name, e)
            return stale
        raise EnrichUnavailable(f"devpi simple {base_url}: {e}") from e

    base_files = base.get("files")
    if not isinstance(base_files, list):
        base_files = []

    metadata_degraded = False
    try:
        meta_by_filename = _fetch_pypi_file_meta(name, pypi_json_url)
    except EnrichUnavailable as e:
        # The base index IS reachable; only the optional upload-time/size source
        # is down. Prefer a stale enriched doc (keeps upload-time), else serve
        # the base list un-stamped rather than turning a metadata blip into a
        # total install outage for plain `pip/uv install <public pkg>`.
        stale = _enrich_cache.get_stale(cache_key, STALE_MAX_SECONDS)
        if stale is not None:
            log.warning("pypi metadata unavailable for %s (%s); serving stale enriched", name, e)
            return stale
        log.warning("pypi metadata unavailable for %s (%s); serving base index without upload-time", name, e)
        meta_by_filename = {}
        metadata_degraded = True

    files: list[dict] = []
    versions: list[str] = []
    missing = 0
    for entry in base_files:
        if not isinstance(entry, dict):
            continue
        filename = entry.get("filename")
        # Preserve the base entry verbatim (filename/url/hashes/requires-python/
        # yanked/etc.) then layer the PEP 700 fields on top.
        out = dict(entry)
        extra = meta_by_filename.get(filename) if isinstance(filename, str) else None
        if extra:
            for key, value in extra.items():
                out.setdefault(key, value)
        if isinstance(filename, str):
            if "upload-time" not in out:
                missing += 1
            ver = version_from_filename(filename, name)
            if ver:
                versions.append(ver)
        files.append(out)

    if missing:
        log.info("enrich_devpi %s: %d/%d files without a PyPI upload-time match", name, missing, len(files))

    doc = build_v1_1(base.get("name", name), files, versions)
    # Only cache a fully-enriched document; a metadata-degraded list must not be
    # pinned for the whole TTL (next request retries PyPI JSON).
    if not metadata_degraded:
        _enrich_cache.put(cache_key, doc)
    return doc


# ---- private (Gitea) ------------------------------------------------------------

class _GiteaSimpleParser(HTMLParser):
    """Scrape (url, filename, sha256) tuples from Gitea's PEP 503 HTML page.

    Each anchor is
      <a href=".../pypi/files/<name>/<version>/<filename>#sha256=...">filename</a>
    """

    def __init__(self):
        super().__init__()
        self.links: list[dict] = []
        self._pending: dict | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        attrd = dict(attrs)
        href = attrd.get("href")
        if not href:
            return
        url, _, fragment = href.partition("#")
        hashes: dict[str, str] = {}
        if fragment.startswith("sha256="):
            hashes["sha256"] = fragment[len("sha256="):]
        link: dict = {"url": url, "hashes": hashes}
        # Preserve the PEP 503 link attributes Gitea emits on the anchor so the
        # JSON path keeps the same install-time filters the HTML path gives pip:
        # data-requires-python gates incompatible interpreters; data-yanked
        # (PEP 592) withdraws a release. Dropping requires-python would let a
        # JSON-capable installer select a wheel its Python cannot run.
        requires_python = attrd.get("data-requires-python")
        if requires_python:
            link["requires-python"] = requires_python
        if "data-yanked" in attrd:
            yanked = attrd.get("data-yanked")
            # Empty/no value -> True; a value -> the reason string.
            link["yanked"] = yanked if yanked else True
        self._pending = link
        self._text = []

    def handle_data(self, data):
        if self._pending is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag != "a" or self._pending is None:
            return
        text = "".join(self._text).strip()
        url = self._pending["url"]
        # filename is the anchor text when present, else the last URL segment.
        filename = text or urllib.parse.unquote(url.rstrip("/").rsplit("/", 1)[-1])
        version = _version_from_gitea_url(url)
        self._pending["filename"] = filename
        self._pending["version"] = version
        self.links.append(self._pending)
        self._pending = None
        self._text = []


_GITEA_FILES_RE = re.compile(r"/pypi/files/[^/]+/([^/]+)/[^/]+/?$")


def _version_from_gitea_url(url: str) -> str | None:
    path = urllib.parse.urlsplit(url).path
    m = _GITEA_FILES_RE.search(path)
    return urllib.parse.unquote(m.group(1)) if m else None


def _gitea_version_meta(
    name: str, version: str, gitea_url: str, namespace: str, authorization: str
) -> tuple[str | None, dict[str, int]]:
    """Per-version (created_at, {filename: size}) from Gitea's package API.

    created_at is the version-level upload time (the per-file endpoint omits it);
    size is mandatory in PEP 700 v1.1, and Gitea exposes it per file via the
    `.../files` endpoint (each PackageFile has name/size/sha256). Both are
    immutable once published, so the joined result is cached together for the
    long CREATED_AT_TTL. A miss/outage on either call degrades only that field
    (the file is still served, just without that optional/mandatory annotation).
    """
    cache_key = (namespace, name, version)
    cached = _created_at_cache.get(cache_key)
    if cached is not None:
        iso, sizes = cached  # cached ("", {}) sentinel means "looked up, none"
        return (iso or None), sizes

    base = (
        f"{gitea_url.rstrip('/')}/api/v1/packages/"
        f"{urllib.parse.quote(namespace)}/pypi/{urllib.parse.quote(name)}/{urllib.parse.quote(version)}"
    )
    headers = {"Accept": "application/json"}
    if authorization:
        headers["Authorization"] = authorization

    iso: str | None = None
    try:
        data = json.loads(_get(base, headers))
        created_at = data.get("created_at") if isinstance(data, dict) else None
        iso = normalize_to_iso_z(created_at) if isinstance(created_at, str) else None
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        log.warning("gitea version created_at unavailable for %s/%s %s: %s", namespace, name, version, e)

    sizes: dict[str, int] = {}
    try:
        files_data = json.loads(_get(base + "/files", headers))
        if isinstance(files_data, list):
            for item in files_data:
                if not isinstance(item, dict):
                    continue
                fn = item.get("name")
                sz = item.get("size")
                if isinstance(fn, str) and isinstance(sz, int) and sz >= 0:
                    sizes[fn] = sz
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        log.warning("gitea version files unavailable for %s/%s %s: %s", namespace, name, version, e)

    _created_at_cache.put(cache_key, (iso or "", sizes))
    return iso, sizes


def enrich_gitea(name: str, gitea_url: str, namespace: str, authorization: str) -> dict | None:
    """Build a v1.1 document for a PRIVATE package from Gitea.

    Gitea only serves PEP 503 HTML and ignores the JSON Accept header, so we
    scrape its simple page for (url, filename, sha256) and stamp each file with
    its version's created_at (per-version granularity: every file of a version
    shares the version upload time). For the normal case — a version published
    atomically (twine/uv upload) — this equals the real upload time. The one
    edge case is a file ADDED to an existing version later: it inherits that
    version's earlier created_at, so a `--uploaded-prior-to` client could select
    it even if its own upload was after the cutoff. Rare given Artea's publish
    model, but it is not an absolute guarantee. Each file also carries its
    per-file `size` from Gitea's package-files API (PEP 700 v1.1 requires `size`).

    Returns None when Gitea 404s (no such private package), which the gateway
    turns into a public-devpi fall-through to preserve the precedence contract.

    Not document-cached: the expensive per-version created_at+size fan-out is
    already memoized in _created_at_cache, so a re-scrape is cheap; an enriched
    private doc has no safe stale-serve path (unlike public), so caching the
    whole document would only add eviction bookkeeping for no win.
    """
    base_url = (
        f"{gitea_url.rstrip('/')}/api/packages/"
        f"{urllib.parse.quote(namespace)}/pypi/simple/{urllib.parse.quote(name)}/"
    )
    headers = {"Accept": "text/html"}
    if authorization:
        headers["Authorization"] = authorization
    try:
        html = _get(base_url, headers).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise EnrichUnavailable(f"gitea simple {base_url}: HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise EnrichUnavailable(f"gitea simple {base_url}: {e}") from e

    parser = _GiteaSimpleParser()
    parser.feed(html)

    # Resolve created_at + per-file sizes once per version (cache also dedupes
    # across files of the same version).
    meta_by_version: dict[str, tuple[str | None, dict[str, int]]] = {}
    files: list[dict] = []
    versions: list[str] = []
    for link in parser.links:
        version = link.get("version")
        out: dict = {"filename": link["filename"], "url": link["url"]}
        if link.get("hashes"):
            out["hashes"] = link["hashes"]
        if link.get("requires-python"):
            out["requires-python"] = link["requires-python"]
        if "yanked" in link:
            out["yanked"] = link["yanked"]
        if version:
            if version not in meta_by_version:
                meta_by_version[version] = _gitea_version_meta(
                    name, version, gitea_url, namespace, authorization
                )
                versions.append(version)
            iso, sizes = meta_by_version[version]
            if iso:
                out["upload-time"] = iso
            size = sizes.get(link["filename"])
            if isinstance(size, int):
                out["size"] = size
        files.append(out)

    return build_v1_1(name, files, versions)
