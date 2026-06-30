"""Unit tests for the PEP 700 upload-time enrichment core (policy_sync.enrich).

Stdlib only: real in-process HTTP stubs for devpi, PyPI JSON, and Gitea so the
join/parse/fail-closed logic is exercised end to end without network access.
"""

import json
import threading

import pytest

from policy_sync import enrich
from tests._stub import StubServer, _StubHandler
from tests._stub import reply as _reply


# ---- in-process stub upstream ---------------------------------------------------

class _Stub(StubServer):
    """A tiny router: register (method, path-prefix) -> handler(self) callbacks."""

    def __init__(self):
        self.routes: list[tuple[str, str, object]] = []
        super().__init__()

    def _build_handler(self):
        stub = self

        class Handler(_StubHandler):
            def do_GET(self):  # noqa: N802
                stub.requests.append({
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "accept": self.headers.get("Accept"),
                })
                for method, prefix, fn in stub.routes:
                    if method == "GET" and self.path.split("?")[0].startswith(prefix):
                        fn(self)
                        return
                self.send_error(404)

        return Handler

    def route(self, prefix, fn):
        self.routes.append(("GET", prefix, fn))


@pytest.fixture(autouse=True)
def _clear_caches():
    # enrich.py caches the enriched dict; start each test from a clean slate.
    enrich._enrich_cache._data.clear()
    enrich._created_at_cache._data.clear()
    yield


@pytest.fixture
def stub():
    s = _Stub()
    s.start()
    yield s
    s.stop()


# ---- public (devpi) path --------------------------------------------------------

def _devpi_simple(files):
    """A PEP 691 v1.0 page like devpi's constrained index returns."""
    return json.dumps({"meta": {"api-version": "1.0"}, "name": "six", "files": files})


def _devpi_meta(file_meta, metadata_available=True):
    """The devpi /+artea/project-meta payload: filename -> per-file metadata.

    Mirrors what the devpi age-gate plugin serves from the pypi.org JSON it
    already parsed: {upload-time (raw, byte-identical), size?, yanked?, version}.
    """
    return json.dumps({"file_meta": file_meta, "metadata_available": metadata_available})


def test_devpi_join_annotates_upload_time_and_size(stub):
    # devpi lists ONE file; project-meta has that file plus another (B) that must
    # NOT leak in (the constrained list is authoritative for the file set).
    fileA = "six-1.0.0-py3-none-any.whl"
    fileB = "six-2.0.0-py3-none-any.whl"
    # requires-python now rides on devpi's verbatim base +simple entry (PEP 691),
    # not the per-file metadata endpoint — set it on the base file accordingly.
    stub.route("/root/constrained/+simple/six/", lambda h: _reply(
        h, 200, _devpi_simple([{"filename": fileA, "url": "http://x/six-1.0.0...whl",
                                "hashes": {"sha256": "abc"}, "requires-python": ">=3.6"}]),
        "application/vnd.pypi.simple.v1+json"))
    stub.route("/+artea/project-meta", lambda h: _reply(h, 200, _devpi_meta({
        fileA: {"upload-time": "2023-06-15T10:23:45.123456Z", "size": 12345, "version": "1.0.0"},
        fileB: {"upload-time": "2024-01-01T00:00:00.000000Z", "size": 999, "version": "2.0.0"},
    })))

    doc = enrich.enrich_devpi("six", stub.url)

    assert doc["meta"]["api-version"] == "1.1"
    assert doc["name"] == "six"
    assert len(doc["files"]) == 1, "only devpi's file set is served"
    f = doc["files"][0]
    assert f["filename"] == fileA
    assert f["upload-time"] == "2023-06-15T10:23:45.123456Z"
    assert f["size"] == 12345
    assert f["requires-python"] == ">=3.6"
    assert "yanked" not in f, "false yanked is not surfaced"
    assert f["hashes"] == {"sha256": "abc"}  # base entry preserved verbatim
    assert "__version" not in f, "internal version key must not leak into output"
    assert "1.0.0" in doc["versions"]
    assert "2.0.0" not in doc["versions"], "version not in the served file set is absent"


def test_devpi_preserves_pep658_core_metadata(stub):
    # PEP 658/714: when devpi's constrained index advertises `core-metadata` for a
    # wheel (mirror_provides_core_metadata on root/pypi), the enriched v1.1 JSON
    # must carry it through unchanged so pip/uv can do a metadata-only resolve.
    fname = "six-1.0.0-py3-none-any.whl"
    stub.route("/root/constrained/+simple/six/", lambda h: _reply(
        h, 200, _devpi_simple([{"filename": fname, "url": "http://x/six-1.0.0...whl",
                                "hashes": {"sha256": "abc"}, "core-metadata": True}]),
        "application/vnd.pypi.simple.v1+json"))
    stub.route("/+artea/project-meta", lambda h: _reply(h, 200, _devpi_meta({
        fname: {"upload-time": "2023-06-15T10:23:45.123456Z", "size": 9, "version": "1.0.0"},
    })))

    doc = enrich.enrich_devpi("six", stub.url)
    assert doc["files"][0]["core-metadata"] is True
    # and the PEP 700 layering still happened on the same file
    assert doc["files"][0]["upload-time"] == "2023-06-15T10:23:45.123456Z"


def test_devpi_ignores_bool_size_metadata(stub):
    fname = "six-1.0.0-py3-none-any.whl"
    stub.route("/root/constrained/+simple/six/", lambda h: _reply(
        h, 200, _devpi_simple([{"filename": fname, "url": "http://x/f.whl", "hashes": {}}]),
        "application/vnd.pypi.simple.v1+json"))
    stub.route("/+artea/project-meta", lambda h: _reply(h, 200, _devpi_meta({
        fname: {
            "upload-time": "2023-06-15T10:23:45.123456Z",
            "size": True,
            "version": "1.0.0",
        },
    })))

    doc = enrich.enrich_devpi("six", stub.url)
    assert doc["files"][0]["upload-time"] == "2023-06-15T10:23:45.123456Z"
    assert "size" not in doc["files"][0]


def test_devpi_versions_use_authoritative_release_key_not_filename(stub):
    # The base file's name embeds a version string ("1.0.0") that the filename
    # heuristic would extract, but PyPI's authoritative release KEY normalizes it
    # to a different canonical form ("1.0.0.post1+local"). versions[] must reflect
    # the authoritative key, NOT the heuristic-extracted "1.0.0".
    fname = "weird_pkg-1.0.0-py3-none-any.whl"
    stub.route("/root/constrained/+simple/weird-pkg/", lambda h: _reply(
        h, 200, json.dumps({"meta": {"api-version": "1.0"}, "name": "weird-pkg",
                            "files": [{"filename": fname, "url": "http://x/f.whl", "hashes": {}}]}),
        "application/vnd.pypi.simple.v1+json"))
    stub.route("/+artea/project-meta", lambda h: _reply(h, 200, _devpi_meta({
        fname: {"upload-time": "2023-06-15T10:23:45.123456Z", "version": "1.0.0.post1+local"},
    })))

    doc = enrich.enrich_devpi("weird-pkg", stub.url)
    assert doc["versions"] == ["1.0.0.post1+local"], "authoritative release key, not the heuristic 1.0.0"
    # heuristic would have produced "1.0.0"; prove it did not
    assert "1.0.0" not in doc["versions"]
    assert "__version" not in doc["files"][0], "internal version key must not leak into output"


def test_devpi_version_falls_back_to_filename_when_no_pypi_match(stub):
    # A devpi file with no PyPI-JSON entry must still contribute a version via the
    # filename heuristic fallback (the contract still relies on it).
    fname = "six-9.9.9-py3-none-any.whl"
    stub.route("/root/constrained/+simple/six/", lambda h: _reply(
        h, 200, _devpi_simple([{"filename": fname, "url": "http://x/f.whl", "hashes": {}}]),
        "application/vnd.pypi.simple.v1+json"))
    stub.route("/+artea/project-meta", lambda h: _reply(h, 200, _devpi_meta({
        "six-1.0.0-py3-none-any.whl": {"upload-time": "2023-06-15T10:23:45.123456Z", "version": "1.0.0"},
    })))

    doc = enrich.enrich_devpi("six", stub.url)
    assert doc["versions"] == ["9.9.9"], "no PyPI match -> filename heuristic fallback"


def test_devpi_filename_miss_keeps_file_without_upload_time(stub):
    # devpi lists a file PyPI JSON has no entry for -> file stays, no upload-time,
    # still bumped to api-version 1.1.
    fname = "six-9.9.9-py3-none-any.whl"
    stub.route("/root/constrained/+simple/six/", lambda h: _reply(
        h, 200, _devpi_simple([{"filename": fname, "url": "http://x/f.whl", "hashes": {}}]),
        "application/vnd.pypi.simple.v1+json"))
    stub.route("/+artea/project-meta", lambda h: _reply(h, 200, _devpi_meta({
        "six-1.0.0-py3-none-any.whl": {"upload-time": "2023-06-15T10:23:45.123456Z", "version": "1.0.0"},
    })))

    doc = enrich.enrich_devpi("six", stub.url)
    assert doc["meta"]["api-version"] == "1.1"
    assert len(doc["files"]) == 1
    assert "upload-time" not in doc["files"][0]


def test_devpi_meta_unavailable_serves_base_index_without_upload_time(stub):
    # devpi list ok, but the project-meta endpoint 500s and no cache exists. The
    # base index IS reachable, so we must NOT 502 a plain `pip/uv install <public
    # pkg>`: serve the installable v1.1 list un-stamped. upload-time is spec-
    # optional, and a time-filtering client that needs it simply won't match the
    # un-stamped file.
    stub.route("/root/constrained/+simple/six/", lambda h: _reply(
        h, 200, _devpi_simple([{"filename": "six-1.0.0-py3-none-any.whl", "url": "http://x", "hashes": {}}]),
        "application/vnd.pypi.simple.v1+json"))
    stub.route("/+artea/project-meta", lambda h: _reply(h, 500, "boom", "text/plain"))

    doc = enrich.enrich_devpi("six", stub.url)
    assert doc["meta"]["api-version"] == "1.1"
    assert len(doc["files"]) == 1
    assert doc["files"][0]["filename"] == "six-1.0.0-py3-none-any.whl"
    assert "upload-time" not in doc["files"][0]


def test_devpi_meta_empty_on_pypi_outage_serves_base_index(stub):
    # devpi degrades pypi.org outages to a 200, but marks metadata unavailable
    # so policy-sync serves the installable base list un-stamped without caching
    # that degraded document.
    state = {"meta_ok": False}
    stub.route("/root/constrained/+simple/six/", lambda h: _reply(
        h, 200, _devpi_simple([{"filename": "six-1.0.0-py3-none-any.whl", "url": "http://x", "hashes": {}}]),
        "application/vnd.pypi.simple.v1+json"))

    def meta(h):
        if state["meta_ok"]:
            _reply(h, 200, _devpi_meta({
                "six-1.0.0-py3-none-any.whl": {"upload-time": "2023-06-15T10:23:45.123456Z", "version": "1.0.0"}}))
        else:
            _reply(h, 200, _devpi_meta({}, metadata_available=False))
    stub.route("/+artea/project-meta", meta)

    doc = enrich.enrich_devpi("six", stub.url)
    assert doc["meta"]["api-version"] == "1.1"
    assert len(doc["files"]) == 1
    assert "upload-time" not in doc["files"][0]
    state["meta_ok"] = True
    recovered = enrich.enrich_devpi("six", stub.url)
    assert recovered["files"][0]["upload-time"] == "2023-06-15T10:23:45.123456Z"


def test_devpi_meta_non_object_payload_serves_base_index(stub):
    # A malformed-but-valid JSON payload from project-meta is metadata degradation,
    # not an enriched-index outage: serve the base list un-stamped.
    stub.route("/root/constrained/+simple/six/", lambda h: _reply(
        h, 200, _devpi_simple([{"filename": "six-1.0.0-py3-none-any.whl", "url": "http://x", "hashes": {}}]),
        "application/vnd.pypi.simple.v1+json"))
    stub.route("/+artea/project-meta", lambda h: _reply(h, 200, "[]", "application/json"))

    doc = enrich.enrich_devpi("six", stub.url)
    assert doc["meta"]["api-version"] == "1.1"
    assert len(doc["files"]) == 1
    assert "upload-time" not in doc["files"][0]


def test_devpi_meta_non_object_file_meta_serves_base_index(stub):
    stub.route("/root/constrained/+simple/six/", lambda h: _reply(
        h, 200, _devpi_simple([{"filename": "six-1.0.0-py3-none-any.whl", "url": "http://x", "hashes": {}}]),
        "application/vnd.pypi.simple.v1+json"))
    stub.route("/+artea/project-meta", lambda h: _reply(
        h, 200, json.dumps({"file_meta": [], "metadata_available": True}), "application/json"))

    doc = enrich.enrich_devpi("six", stub.url)
    assert doc["meta"]["api-version"] == "1.1"
    assert len(doc["files"]) == 1
    assert "upload-time" not in doc["files"][0]


def test_devpi_base_list_unavailable_fails_closed(stub):
    # The base index is down (5xx) and no cache exists -> EnrichUnavailable
    # (a synthesized empty list would look like "no such package"). This is the
    # kind of failure that becomes a gateway 502; metadata outages do not.
    # A genuine 404 is a DIFFERENT case (no such project -> EnrichNotFound below).
    stub.route("/+artea/project-meta", lambda h: _reply(h, 200, _devpi_meta({})))  # meta up
    stub.route("/root/constrained/+simple/six/", lambda h: _reply(h, 503, "down", "text/plain"))
    with pytest.raises(enrich.EnrichUnavailable):
        enrich.enrich_devpi("six", stub.url)


def test_devpi_base_list_404_is_not_found_not_unavailable(stub):
    # Absent from the public mirror too: the constrained index 404s exactly as
    # the HTML fallback does. That is a real "no such project" -> EnrichNotFound
    # (the server turns it into 404 "no candidates"), NOT EnrichUnavailable
    # (which would 502 and make JSON pip/uv retry a missing package). A 404 must
    # never serve stale, so even a warm cache does not mask it.
    stub.route("/+artea/project-meta", lambda h: _reply(h, 200, _devpi_meta({})))  # meta up
    stub.route("/root/constrained/+simple/six/", lambda h: _reply(h, 404, "nope", "text/plain"))
    with pytest.raises(enrich.EnrichNotFound):
        enrich.enrich_devpi("six", stub.url)


def test_devpi_metadata_degraded_doc_is_not_cached(stub):
    # A metadata-degraded list must not be pinned for the TTL: once the project-
    # meta endpoint recovers, the very next request must produce the enriched
    # (stamped) list.
    state = {"meta_ok": False}
    stub.route("/root/constrained/+simple/six/", lambda h: _reply(
        h, 200, _devpi_simple([{"filename": "six-1.0.0-py3-none-any.whl", "url": "http://x", "hashes": {}}]),
        "application/vnd.pypi.simple.v1+json"))

    def meta(h):
        if state["meta_ok"]:
            _reply(h, 200, _devpi_meta({
                "six-1.0.0-py3-none-any.whl": {"upload-time": "2023-06-15T10:23:45.123456Z", "version": "1.0.0"}}))
        else:
            _reply(h, 500, "down", "text/plain")
    stub.route("/+artea/project-meta", meta)

    degraded = enrich.enrich_devpi("six", stub.url)
    assert "upload-time" not in degraded["files"][0]
    state["meta_ok"] = True
    recovered = enrich.enrich_devpi("six", stub.url)
    assert recovered["files"][0]["upload-time"] == "2023-06-15T10:23:45.123456Z"


def test_devpi_stale_cache_served_when_meta_unavailable(stub):
    # First call succeeds and caches; then the project-meta endpoint goes down. We
    # age the cached entry past the read-through TTL so the fresh-collapse misses
    # and the stale-serve path is the one exercised here.
    state = {"meta_ok": True}
    stub.route("/root/constrained/+simple/six/", lambda h: _reply(
        h, 200, _devpi_simple([{"filename": "six-1.0.0-py3-none-any.whl", "url": "http://x", "hashes": {}}]),
        "application/vnd.pypi.simple.v1+json"))

    def meta(h):
        if state["meta_ok"]:
            _reply(h, 200, _devpi_meta({
                "six-1.0.0-py3-none-any.whl": {"upload-time": "2023-06-15T10:23:45.123456Z", "version": "1.0.0"}}))
        else:
            _reply(h, 500, "down", "text/plain")
    stub.route("/+artea/project-meta", meta)

    first = enrich.enrich_devpi("six", stub.url)
    assert first["files"][0]["upload-time"] == "2023-06-15T10:23:45.123456Z"
    # Age the cached doc past ENRICH_TTL_SECONDS (still within STALE_MAX_SECONDS)
    # so the within-TTL read-through misses and the stale path is taken.
    key = ("devpi", "six")
    stored_at, value = enrich._enrich_cache._data[key]
    enrich._enrich_cache._data[key] = (stored_at - enrich.ENRICH_TTL_SECONDS - 1, value)
    state["meta_ok"] = False
    second = enrich.enrich_devpi("six", stub.url)  # served from stale cache
    assert second["files"][0]["upload-time"] == "2023-06-15T10:23:45.123456Z"


def test_devpi_within_ttl_read_through_collapses_without_refetch(stub):
    # TASK B: a within-TTL enriched doc is served without re-hitting devpi or the
    # meta endpoint, so repeat/concurrent same-package resolves collapse.
    calls = {"base": 0, "meta": 0}

    def base(h):
        calls["base"] += 1
        _reply(h, 200, _devpi_simple([{"filename": "six-1.0.0-py3-none-any.whl", "url": "http://x", "hashes": {}}]),
               "application/vnd.pypi.simple.v1+json")

    def meta(h):
        calls["meta"] += 1
        _reply(h, 200, _devpi_meta({
            "six-1.0.0-py3-none-any.whl": {"upload-time": "2023-06-15T10:23:45.123456Z", "version": "1.0.0"}}))
    stub.route("/root/constrained/+simple/six/", base)
    stub.route("/+artea/project-meta", meta)

    first = enrich.enrich_devpi("six", stub.url)
    second = enrich.enrich_devpi("six", stub.url)
    assert second == first
    assert calls == {"base": 1, "meta": 1}, "second resolve served from the fresh cache"


# ---- private (Gitea) path -------------------------------------------------------

def _gitea_html(entries):
    """entries: list of (filename, version, sha256)."""
    body = ["<!DOCTYPE html><html><body>"]
    for filename, version, sha in entries:
        href = f"/api/packages/artea/pypi/files/demo/{version}/{filename}#sha256={sha}"
        body.append(f'<a href="{href}">{filename}</a>')
    body.append("</body></html>")
    return "\n".join(body)


def _gitea_files(entries):
    """entries: list of (name, size, sha256) like the Gitea .../files endpoint."""
    return json.dumps([
        {"id": i, "name": name, "size": size, "sha256": sha}
        for i, (name, size, sha) in enumerate(entries)
    ])


def test_gitea_per_version_created_at_size_and_sha256(stub):
    # NOTE: register the longer "/files" prefix first — the stub matches by
    # startswith, and the version endpoint is a prefix of the files endpoint.
    stub.route("/api/v1/packages/artea/pypi/demo/0.0.1/files", lambda h: _reply(
        h, 200, _gitea_files([
            ("demo-0.0.1-py3-none-any.whl", 1234, "deadbeef"),
            ("demo-0.0.1.tar.gz", 5678, "cafef00d"),
        ])))
    stub.route("/api/packages/artea/pypi/simple/demo/", lambda h: _reply(
        h, 200, _gitea_html([
            ("demo-0.0.1-py3-none-any.whl", "0.0.1", "deadbeef"),
            ("demo-0.0.1.tar.gz", "0.0.1", "cafef00d"),
        ]), "text/html"))
    stub.route("/api/v1/packages/artea/pypi/demo/0.0.1", lambda h: _reply(
        h, 200, json.dumps({"created_at": "2026-06-16T02:21:17Z"})))

    doc = enrich.enrich_gitea("demo", stub.url, "artea", "Basic abc")
    assert doc["meta"]["api-version"] == "1.1"
    assert len(doc["files"]) == 2
    for f in doc["files"]:
        # second-precision created_at padded to canonical microsecond Z form
        assert f["upload-time"] == "2026-06-16T02:21:17.000000Z"
        # PEP 700 v1.1: size is mandatory; sourced from Gitea's files endpoint
        assert isinstance(f["size"], int)
    sizes = {f["filename"]: f["size"] for f in doc["files"]}
    assert sizes == {"demo-0.0.1-py3-none-any.whl": 1234, "demo-0.0.1.tar.gz": 5678}
    assert doc["files"][0]["hashes"] == {"sha256": "deadbeef"}
    assert doc["versions"] == ["0.0.1"]
    # version metadata fetched once per version, not once per file (cache dedupe)
    version_calls = [r for r in stub.requests
                     if r["path"] == "/api/v1/packages/artea/pypi/demo/0.0.1"]
    assert len(version_calls) == 1
    files_calls = [r for r in stub.requests
                   if r["path"] == "/api/v1/packages/artea/pypi/demo/0.0.1/files"]
    assert len(files_calls) == 1


def test_gitea_carries_requires_python_and_yanked_from_anchor(stub):
    # Gitea emits data-requires-python (and PEP 592 data-yanked) on its PEP 503
    # anchors; the JSON path must preserve them so a JSON-capable installer keeps
    # the same interpreter/yank filters the byte-for-byte HTML path gives pip.
    # The attribute value arrives HTML-escaped (Go html/template), so the parser
    # must surface the unescaped ">=3.8".
    html = (
        "<!DOCTYPE html><html><body>"
        '<a href="/api/packages/artea/pypi/files/demo/0.0.1/demo-0.0.1.tar.gz#sha256=aa"'
        ' data-requires-python="&gt;=3.8" data-yanked="security">demo-0.0.1.tar.gz</a>'
        '<a href="/api/packages/artea/pypi/files/demo/0.0.2/demo-0.0.2.tar.gz#sha256=bb">'
        "demo-0.0.2.tar.gz</a>"
        "</body></html>"
    )
    stub.route("/api/packages/artea/pypi/simple/demo/", lambda h: _reply(h, 200, html, "text/html"))
    # version metadata is irrelevant here; let it 500 (file still carries attrs)
    stub.route("/api/v1/packages/artea/pypi/demo/", lambda h: _reply(h, 500, "x", "text/plain"))

    doc = enrich.enrich_gitea("demo", stub.url, "artea", "Basic abc")
    by_name = {f["filename"]: f for f in doc["files"]}
    assert by_name["demo-0.0.1.tar.gz"]["requires-python"] == ">=3.8"
    assert by_name["demo-0.0.1.tar.gz"]["yanked"] == "security"
    # a file without the attributes must not gain them
    assert "requires-python" not in by_name["demo-0.0.2.tar.gz"]
    assert "yanked" not in by_name["demo-0.0.2.tar.gz"]


def test_gitea_404_returns_none(stub):
    # no route registered -> the stub 404s -> enrich_gitea returns None so the
    # gateway falls through to the public mirror.
    assert enrich.enrich_gitea("missing", stub.url, "artea", "Basic abc") is None


def test_gitea_forwards_authorization_never_service_token(stub):
    stub.route("/api/v1/packages/artea/pypi/demo/0.0.1/files", lambda h: _reply(
        h, 200, _gitea_files([("demo-0.0.1.tar.gz", 42, "aa")])))
    stub.route("/api/packages/artea/pypi/simple/demo/", lambda h: _reply(
        h, 200, _gitea_html([("demo-0.0.1.tar.gz", "0.0.1", "aa")]), "text/html"))
    stub.route("/api/v1/packages/artea/pypi/demo/0.0.1", lambda h: _reply(
        h, 200, json.dumps({"created_at": "2026-06-16T02:21:17Z"})))
    enrich.enrich_gitea("demo", stub.url, "artea", "Basic client-cred")
    auths = {r["authorization"] for r in stub.requests}
    assert "Basic client-cred" in auths
    assert all(a == "Basic client-cred" for a in auths if a is not None)


def test_gitea_missing_created_at_keeps_file_without_upload_time(stub):
    stub.route("/api/packages/artea/pypi/simple/demo/", lambda h: _reply(
        h, 200, _gitea_html([("demo-0.0.1.tar.gz", "0.0.1", "aa")]), "text/html"))
    # both version-metadata endpoints 500 -> file kept, no upload-time, no size
    stub.route("/api/v1/packages/artea/pypi/demo/0.0.1/files", lambda h: _reply(h, 500, "down", "text/plain"))
    stub.route("/api/v1/packages/artea/pypi/demo/0.0.1", lambda h: _reply(h, 500, "down", "text/plain"))
    doc = enrich.enrich_gitea("demo", stub.url, "artea", "Basic abc")
    assert doc["meta"]["api-version"] == "1.1"
    assert len(doc["files"]) == 1
    assert "upload-time" not in doc["files"][0]
    assert "size" not in doc["files"][0]


# ---- helpers --------------------------------------------------------------------

def test_normalize_to_iso_z_pads_microseconds():
    assert enrich.normalize_to_iso_z("2026-06-16T02:21:17Z") == "2026-06-16T02:21:17.000000Z"
    assert enrich.normalize_to_iso_z("2026-06-13T02:23:35.412579Z") == "2026-06-13T02:23:35.412579Z"
    assert enrich.normalize_to_iso_z("not-a-date") is None


def test_version_from_filename():
    assert enrich.version_from_filename("six-1.0.0-py3-none-any.whl", "six") == "1.0.0"
    assert enrich.version_from_filename("six-1.0.0.tar.gz", "six") == "1.0.0"
    assert enrich.version_from_filename("my_pkg-2.3.tar.gz", "my-pkg") == "2.3"


def test_build_v1_1_envelope():
    doc = enrich.build_v1_1("demo", [{"filename": "a"}], ["1.0", "1.0", "0.9"])
    assert doc["meta"] == {"api-version": "1.1"}
    assert doc["name"] == "demo"
    assert doc["versions"] == ["0.9", "1.0"]  # sorted + de-duped


# ---- server endpoint contract ---------------------------------------------------

def _make_enrich_server(stub):
    """A real PolicySyncHTTPServer wired to the stub as both gitea and devpi."""
    import urllib.error
    import urllib.request
    from policy_sync.config import Config
    from policy_sync.server import PolicySyncHandler, PolicySyncHTTPServer, SyncState
    from policy_sync.store import PolicyStore

    cfg = Config(
        gitea_url=stub.url, sync_token="t", webhook_secret="s", policy_repo="artea/registry-policy",
        policy_file_path="", upstream_policy_file_path="", pypi_policy_file_path="", parsed_policy_file_path="",
        devpi_url=stub.url, devpi_root_password="pw", poll_interval=300,
        namespace="artea", pypi_json_url=stub.url,
    )
    httpd = PolicySyncHTTPServer(
        ("127.0.0.1", 0), "s", lambda: None, SyncState(),
        PolicyStore(fallback_path=""), PolicyStore(fallback_path=""), cfg=cfg,
    )
    httpd.daemon_threads = True
    threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.01), daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def get(path, authorization=None):
        req = urllib.request.Request(base + path)
        if authorization:
            req.add_header("Authorization", authorization)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.headers.get("Content-Type"), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type"), e.read()

    return httpd, get


def test_endpoint_devpi_returns_v1_1_with_content_type(stub):
    stub.route("/root/constrained/+simple/six/", lambda h: _reply(
        h, 200, _devpi_simple([{"filename": "six-1.0.0-py3-none-any.whl", "url": "http://x", "hashes": {"sha256": "a"}}]),
        "application/vnd.pypi.simple.v1+json"))
    stub.route("/+artea/project-meta", lambda h: _reply(h, 200, _devpi_meta({
        "six-1.0.0-py3-none-any.whl": {"upload-time": "2023-06-15T10:23:45.123456Z", "version": "1.0.0"}})))
    httpd, get = _make_enrich_server(stub)
    try:
        status, ctype, body = get("/pypi/simple-enrich?upstream=devpi&name=six")
        assert status == 200
        assert ctype == "application/vnd.pypi.simple.v1+json"
        doc = json.loads(body)
        assert doc["meta"]["api-version"] == "1.1"
        assert doc["files"][0]["upload-time"] == "2023-06-15T10:23:45.123456Z"
    finally:
        httpd.shutdown(); httpd.server_close()


def test_endpoint_gitea_miss_returns_404(stub):
    httpd, get = _make_enrich_server(stub)  # no gitea route -> 404
    try:
        status, _, _ = get("/pypi/simple-enrich?upstream=gitea&name=missing", "Basic abc")
        assert status == 404
    finally:
        httpd.shutdown(); httpd.server_close()


def test_endpoint_rejects_bad_params(stub):
    httpd, get = _make_enrich_server(stub)
    try:
        assert get("/pypi/simple-enrich?upstream=bogus&name=six")[0] == 400
        assert get("/pypi/simple-enrich?upstream=devpi&name=Not_Normalized")[0] == 400
        assert get("/pypi/simple-enrich?upstream=devpi&name=../etc")[0] == 400
    finally:
        httpd.shutdown(); httpd.server_close()
