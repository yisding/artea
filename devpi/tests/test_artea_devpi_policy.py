import json
import sys
import time
import tomllib
from pathlib import Path

import pytest

PLUGIN_SRC = Path(__file__).resolve().parents[1] / "artea_devpi_policy" / "src"
sys.path.insert(0, str(PLUGIN_SRC))

from artea_devpi_policy import main  # noqa: E402
from artea_devpi_policy.main import (  # noqa: E402
    ConstrainedStage,
    MetadataUnavailable,
    ProjectMetadata,
    file_age_tween_factory,
    fetch_project_metadata,
    link_entrypath,
    project_meta_view,
    pypi_file_allowed_view,
    parse_iso_duration_seconds,
)
from pyramid.httpexceptions import HTTPBadRequest, HTTPForbidden  # noqa: E402


class FakeStage:
    def __init__(self, ixconfig):
        self.ixconfig = ixconfig


class FakeLink:
    def __init__(self, version, filename, project="six"):
        self.name = project
        self.project = project
        self.version = version
        self.basename = filename


class FakeELink:
    def __init__(self, version, filename):
        self.project = "six"
        self.version = version
        self.basename = filename


class FakeRequest:
    def __init__(self, path_info, registry=None, params=None, matchdict=None):
        self.path_info = path_info
        self.registry = registry or {}
        self.params = params or {}
        self.matchdict = matchdict or {}


def make_stage(min_age="P3D"):
    customizer = ConstrainedStage()
    customizer.stage = FakeStage({
        "constraints": [],
        "min_upstream_age": min_age,
        "pypi_json_url": "https://pypi.example.test/pypi",
    })
    return customizer


def test_plugin_declares_devpi_server_entry_point():
    pyproject = tomllib.loads((PLUGIN_SRC.parent / "pyproject.toml").read_text())

    assert pyproject["project"]["entry-points"]["devpi_server"]["artea-devpi-policy"] == (
        "artea_devpi_policy.main"
    )


def test_parse_iso_duration_seconds():
    assert parse_iso_duration_seconds("P3D") == 3 * 24 * 60 * 60
    assert parse_iso_duration_seconds("PT72H") == 72 * 60 * 60
    assert parse_iso_duration_seconds("P0D") == 0
    with pytest.raises(ValueError):
        parse_iso_duration_seconds("3d")


def test_simple_links_filter_applies_min_upstream_age(monkeypatch):
    now = time.time()
    customizer = make_stage("P3D")
    metadata = ProjectMetadata(
        fetched_at=now,
        files={
            "six-1.0.0-py3-none-any.whl": now - 4 * 24 * 60 * 60,
            "six-2.0.0-py3-none-any.whl": now - 6 * 60 * 60,
        },
        versions={
            "1.0.0": [now - 4 * 24 * 60 * 60],
            "2.0.0": [now - 6 * 60 * 60],
        },
    )
    monkeypatch.setattr(customizer, "_project_metadata", lambda project: metadata)

    result = list(customizer.get_simple_links_filter_iter("six", [
        FakeLink("1.0.0", "six-1.0.0-py3-none-any.whl"),
        FakeLink("2.0.0", "six-2.0.0-py3-none-any.whl"),
    ]))

    assert result == [True, False]


def test_constrain_all_unlisted_project_denies_without_metadata(monkeypatch):
    customizer = make_stage("P3D")
    customizer.stage.ixconfig["constraints"] = ["*"]

    def fail_metadata(project):
        raise AssertionError(f"metadata should not be fetched for {project}")

    monkeypatch.setattr(customizer, "_project_metadata", fail_metadata)

    result = list(customizer.get_simple_links_filter_iter("unlisted", [
        FakeLink("1.0.0", "unlisted-1.0.0-py3-none-any.whl", project="unlisted"),
        FakeLink("2.0.0", "unlisted-2.0.0-py3-none-any.whl", project="unlisted"),
    ]))

    # An unlisted project under a global `*` constraint is denied for every item
    # purely by constrain_all — without any upstream metadata fetch (fail_metadata
    # would raise) — even though the links' versions are otherwise well-formed.
    assert result == [False, False]


def test_simple_links_filter_applies_osv_malicious_verdict(monkeypatch):
    customizer = make_stage("P0D")
    customizer.stage.ixconfig["osv_url"] = "http://policy-sync.example/osv/querybatch"
    monkeypatch.setattr(
        "artea_devpi_policy.main.query_osv_blocked_versions",
        lambda osv_url, project, versions: {"2.0.0"},
    )

    result = list(customizer.get_simple_links_filter_iter("six", [
        FakeLink("1.0.0", "six-1.0.0-py3-none-any.whl"),
        FakeLink("2.0.0", "six-2.0.0-py3-none-any.whl"),
    ]))

    assert result == [True, False]


def test_link_allowed_requires_verifiable_upload_time(monkeypatch):
    # Age gate on, no version constraints: link_allowed must fail closed for a
    # file whose upload time cannot be verified from mirror metadata.
    now = time.time()
    customizer = make_stage("P3D")
    metadata = ProjectMetadata(
        fetched_at=now,
        files={"six-1.0.0-py3-none-any.whl": now - 4 * 24 * 60 * 60},
        versions={},
    )
    monkeypatch.setattr(customizer, "_project_metadata", lambda project: metadata)

    assert customizer.link_allowed("six", FakeLink("1.0.0", "six-1.0.0-py3-none-any.whl")) is True
    assert customizer.link_allowed("six", FakeLink("2.0.0", "six-2.0.0-py3-none-any.whl")) is False


def test_link_allowed_applies_constraints_without_rendering_simple_page():
    customizer = make_stage("P0D")
    customizer.stage.ixconfig["constraints"] = ["six<2"]

    assert customizer.link_allowed("six", FakeLink("1.17.0", "six-1.17.0-py2.py3-none-any.whl")) is True
    assert customizer.link_allowed("six", FakeLink("2.0.0", "six-2.0.0-py3-none-any.whl")) is False
    assert customizer.link_allowed("six", FakeELink("1.17.0", "six-1.17.0-py2.py3-none-any.whl")) is True


def test_link_allowed_applies_osv_malicious_verdict(monkeypatch):
    customizer = make_stage("P0D")
    customizer.stage.ixconfig["osv_url"] = "http://policy-sync.example/osv/querybatch"
    monkeypatch.setattr(
        "artea_devpi_policy.main.query_osv_blocked_versions",
        lambda osv_url, project, versions: {"2.0.0"},
    )

    assert customizer.link_allowed("six", FakeLink("1.0.0", "six-1.0.0-py3-none-any.whl")) is True
    assert customizer.link_allowed("six", FakeLink("2.0.0", "six-2.0.0-py3-none-any.whl")) is False


def test_file_allowed_endpoint_uses_current_constrained_policy():
    customizer = make_stage("P0D")
    customizer.stage.ixconfig["constraints"] = ["six<2"]

    class FakeConstrainedStage:
        def __init__(self, stage_customizer):
            self.customizer = stage_customizer

    class FakeMirrorStage:
        def get_link_from_entrypath(self, path):
            return {
                "root/pypi/+f/472/six-1.17.0-py2.py3-none-any.whl": FakeELink(
                    "1.17.0", "six-1.17.0-py2.py3-none-any.whl"
                ),
                "root/pypi/+f/bad/six-2.0.0-py3-none-any.whl": FakeELink(
                    "2.0.0", "six-2.0.0-py3-none-any.whl"
                ),
            }.get(path)

    class FakeModel:
        def getstage(self, name):
            return {"root/constrained": FakeConstrainedStage(customizer), "root/pypi": FakeMirrorStage()}.get(name)

    class FakeXom:
        model = FakeModel()

    registry = {"xom": FakeXom()}
    allowed = pypi_file_allowed_view(FakeRequest(
        "/+artea/file-allowed",
        registry=registry,
        params={"path": "/root/pypi/+f/472/six-1.17.0-py2.py3-none-any.whl"},
    ))
    assert allowed.status_code == 204

    with pytest.raises(HTTPForbidden):
        pypi_file_allowed_view(FakeRequest(
            "/+artea/file-allowed",
            registry=registry,
            params={"path": "/root/pypi/+f/bad/six-2.0.0-py3-none-any.whl"},
        ))


def test_file_allowed_endpoint_derives_project_from_mirror_link():
    # The project is taken from devpi's mirror link, never from the request, so a
    # file whose name a naive parser would mis-split is still judged correctly.
    customizer = make_stage("P0D")

    class FakeConstrainedStage:
        def __init__(self, stage_customizer):
            self.customizer = stage_customizer

    class TrickyLink:
        project = "backports-tarfile"
        name = "backports-tarfile"
        version = "1.0.0"
        basename = "backports.tarfile-1.0.0.tar.gz"

    class FakeMirrorStage:
        def get_link_from_entrypath(self, path):
            return TrickyLink()

    class FakeModel:
        def getstage(self, name):
            return {"root/constrained": FakeConstrainedStage(customizer), "root/pypi": FakeMirrorStage()}.get(name)

    class FakeXom:
        model = FakeModel()

    allowed = pypi_file_allowed_view(FakeRequest(
        "/+artea/file-allowed",
        registry={"xom": FakeXom()},
        params={"path": "/root/pypi/+f/abc/backports.tarfile-1.0.0.tar.gz"},
    ))
    assert allowed.status_code == 204


def test_link_entrypath_strips_pep658_metadata_suffix():
    whl = "root/pypi/+f/abc/six-1.0.0-py3-none-any.whl"
    assert link_entrypath(whl) == whl  # plain file unchanged
    assert link_entrypath(whl + ".metadata") == whl  # PEP 658 metadata -> wheel


def test_file_allowed_endpoint_gates_pep658_metadata_like_its_wheel():
    # PEP 658: a `<wheel>.metadata` request is allowed iff the wheel is. The mirror
    # only knows the wheel's entrypath (devpi appends `.metadata` for serving but
    # registers no separate link), so the policy must strip the suffix to resolve
    # the link — without that, an allowed wheel's metadata would 403.
    customizer = make_stage("P0D")
    customizer.stage.ixconfig["constraints"] = ["six<2"]

    class FakeConstrainedStage:
        def __init__(self, stage_customizer):
            self.customizer = stage_customizer

    class FakeMirrorStage:
        def get_link_from_entrypath(self, path):
            return {
                "root/pypi/+f/472/six-1.17.0-py2.py3-none-any.whl": FakeELink(
                    "1.17.0", "six-1.17.0-py2.py3-none-any.whl"
                ),
                "root/pypi/+f/bad/six-2.0.0-py3-none-any.whl": FakeELink(
                    "2.0.0", "six-2.0.0-py3-none-any.whl"
                ),
            }.get(path)

    class FakeModel:
        def getstage(self, name):
            return {"root/constrained": FakeConstrainedStage(customizer), "root/pypi": FakeMirrorStage()}.get(name)

    class FakeXom:
        model = FakeModel()

    registry = {"xom": FakeXom()}
    allowed = pypi_file_allowed_view(FakeRequest(
        "/+artea/file-allowed",
        registry=registry,
        params={"path": "/root/pypi/+f/472/six-1.17.0-py2.py3-none-any.whl.metadata"},
    ))
    assert allowed.status_code == 204

    with pytest.raises(HTTPForbidden):
        pypi_file_allowed_view(FakeRequest(
            "/+artea/file-allowed",
            registry=registry,
            params={"path": "/root/pypi/+f/bad/six-2.0.0-py3-none-any.whl.metadata"},
        ))


def test_direct_public_file_tween_gates_pep658_metadata_like_its_wheel(monkeypatch):
    # The age tween intercepts the real `.metadata` serve too (its path is under
    # the mirror file prefix); it must resolve the wheel link and block a too-new
    # file's metadata exactly as it blocks the wheel.
    now = time.time()
    customizer = make_stage("P3D")
    metadata = ProjectMetadata(
        fetched_at=now,
        files={"six-2.0.0-py3-none-any.whl": now - 6 * 60 * 60},
        versions={},
    )
    monkeypatch.setattr(customizer, "_project_metadata", lambda project: metadata)

    class FakeConstrainedStage:
        def __init__(self, stage_customizer):
            self.customizer = stage_customizer

    class FakeMirrorStage:
        def get_link_from_entrypath(self, path):
            # Only the wheel entrypath is known; a `.metadata` lookup that is not
            # stripped returns None and the tween would 403 with a different reason.
            if path == "root/pypi/+f/abc/six-2.0.0-py3-none-any.whl":
                return FakeLink("2.0.0", "six-2.0.0-py3-none-any.whl")
            return None

    class FakeModel:
        def getstage(self, name):
            return {"root/constrained": FakeConstrainedStage(customizer), "root/pypi": FakeMirrorStage()}.get(name)

    class FakeXom:
        model = FakeModel()

    tween = file_age_tween_factory(lambda request: "ok", {"xom": FakeXom()})

    with pytest.raises(HTTPForbidden):
        tween(FakeRequest("/root/pypi/+f/abc/six-2.0.0-py3-none-any.whl.metadata"))


def test_direct_public_file_tween_enforces_min_upstream_age(monkeypatch):
    now = time.time()
    customizer = make_stage("P3D")
    metadata = ProjectMetadata(
        fetched_at=now,
        files={"six-2.0.0-py3-none-any.whl": now - 6 * 60 * 60},
        versions={},
    )
    monkeypatch.setattr(customizer, "_project_metadata", lambda project: metadata)

    class FakeConstrainedStage:
        def __init__(self, stage_customizer):
            self.customizer = stage_customizer

    class FakeMirrorStage:
        def get_link_from_entrypath(self, path):
            return FakeLink("2.0.0", "six-2.0.0-py3-none-any.whl")

    class FakeModel:
        def getstage(self, name):
            return {"root/constrained": FakeConstrainedStage(customizer), "root/pypi": FakeMirrorStage()}.get(name)

    class FakeXom:
        model = FakeModel()

    tween = file_age_tween_factory(lambda request: "ok", {"xom": FakeXom()})

    with pytest.raises(HTTPForbidden):
        tween(FakeRequest("/root/pypi/+f/abc/six-2.0.0-py3-none-any.whl"))


def test_direct_public_file_tween_enforces_osv_malicious_verdict(monkeypatch):
    customizer = make_stage("P0D")
    customizer.stage.ixconfig["osv_url"] = "http://policy-sync.example/osv/querybatch"
    monkeypatch.setattr(
        "artea_devpi_policy.main.query_osv_blocked_versions",
        lambda osv_url, project, versions: {"2.0.0"},
    )

    class FakeConstrainedStage:
        def __init__(self, stage_customizer):
            self.customizer = stage_customizer

    class FakeMirrorStage:
        def get_link_from_entrypath(self, path):
            return FakeLink("2.0.0", "six-2.0.0-py3-none-any.whl")

    class FakeModel:
        def getstage(self, name):
            return {"root/constrained": FakeConstrainedStage(customizer), "root/pypi": FakeMirrorStage()}.get(name)

    class FakeXom:
        model = FakeModel()

    tween = file_age_tween_factory(lambda request: "ok", {"xom": FakeXom()})

    with pytest.raises(HTTPForbidden):
        tween(FakeRequest("/root/pypi/+f/abc/six-2.0.0-py3-none-any.whl"))


# ---- per-file metadata for policy-sync's PEP 700 enrichment ---------------------


class _FakeResp:
    """Minimal urlopen() stand-in: context manager whose read() yields JSON."""

    def __init__(self, payload):
        self._data = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, *a):
        return self._data


def _meta_registry(customizer):
    class FakeConstrainedStage:
        def __init__(self, c):
            self.customizer = c

    class FakeModel:
        def getstage(self, name):
            return {"root/constrained": FakeConstrainedStage(customizer)}.get(name)

    class FakeXom:
        model = FakeModel()

    return {"xom": FakeXom()}


def test_fetch_project_metadata_populates_file_meta(monkeypatch):
    # The same pypi.org JSON the age gate parses now also yields per-file metadata
    # (upload-time/size/yanked/version) for policy-sync — no extra fetch. The epoch
    # `files`/`versions` the age gate reads must stay populated and unchanged.
    main.metadata_cache.clear()
    payload = {"releases": {
        "1.0.0": [{"filename": "six-1.0.0-py3-none-any.whl",
                   "upload_time_iso_8601": "2023-06-15T10:23:45.123456Z",
                   "size": 12345, "requires_python": ">=3.6", "yanked": False}],
        "2.0.0": [{"filename": "six-2.0.0-py3-none-any.whl",
                   "upload_time_iso_8601": "2024-01-01T00:00:00.000000Z",
                   "size": 999, "yanked": "broken"}],
    }}
    monkeypatch.setattr(main.urllib.request, "urlopen",
                        lambda req, timeout=10: _FakeResp(payload))

    meta = fetch_project_metadata("six", "https://pypi.example.test/pypi")

    # upload-time is the RAW PyPI string (byte-identical), version is the releases
    # dict KEY, size carried, false-yanked dropped, truthy-yanked surfaced.
    assert meta.file_meta["six-1.0.0-py3-none-any.whl"] == {
        "upload-time": "2023-06-15T10:23:45.123456Z", "version": "1.0.0", "size": 12345,
    }
    assert "yanked" not in meta.file_meta["six-1.0.0-py3-none-any.whl"]
    assert meta.file_meta["six-2.0.0-py3-none-any.whl"]["yanked"] == "broken"
    assert meta.file_meta["six-2.0.0-py3-none-any.whl"]["version"] == "2.0.0"
    # Age-gate contract unchanged: epoch files + versions still present.
    assert set(meta.files) == {"six-1.0.0-py3-none-any.whl", "six-2.0.0-py3-none-any.whl"}
    assert "1.0.0" in meta.versions and "2.0.0" in meta.versions
    assert all(isinstance(v, float) for v in meta.files.values())


def test_fetch_project_metadata_rejects_malformed_releases(monkeypatch):
    cases = (
        ([], "root must be an object"),
        ({}, "releases must be an object"),
        ({"releases": []}, "releases must be an object"),
    )
    for payload, match in cases:
        main.metadata_cache.clear()

        def fake_urlopen(*_args, payload=payload, **_kwargs):
            return _FakeResp(payload)

        monkeypatch.setattr(main.urllib.request, "urlopen", fake_urlopen)

        with pytest.raises(MetadataUnavailable, match=match):
            fetch_project_metadata("six", "https://pypi.example.test/pypi")


def test_project_meta_endpoint_returns_file_meta():
    customizer = make_stage("P3D")
    metadata = ProjectMetadata(
        fetched_at=time.time(),
        files={"six-1.0.0-py3-none-any.whl": 123.0},
        versions={"1.0.0": [123.0]},
        file_meta={"six-1.0.0-py3-none-any.whl": {
            "upload-time": "2023-06-15T10:23:45.123456Z", "size": 12345, "version": "1.0.0"}},
    )
    customizer._project_metadata = lambda project: metadata

    resp = project_meta_view(FakeRequest(
        "/+artea/project-meta", registry=_meta_registry(customizer), params={"name": "six"}))
    assert resp.status_code == 200
    assert resp.content_type == "application/json"
    body = json.loads(resp.body)
    assert body["metadata_available"] is True
    assert body["file_meta"]["six-1.0.0-py3-none-any.whl"] == {
        "upload-time": "2023-06-15T10:23:45.123456Z", "size": 12345, "version": "1.0.0"}


def test_project_meta_endpoint_normalizes_name():
    # The view normalizes the requested name before looking up metadata, so a
    # caller's un-normalized form still resolves the right project.
    seen = {}
    customizer = make_stage("P3D")

    def fake_meta(project):
        seen["project"] = project
        return ProjectMetadata(fetched_at=time.time(), files={}, versions={}, file_meta={})
    customizer._project_metadata = fake_meta

    project_meta_view(FakeRequest(
        "/+artea/project-meta", registry=_meta_registry(customizer), params={"name": "Flask_Login"}))
    assert seen["project"] == "flask-login"


def test_project_meta_endpoint_empty_file_meta_on_pypi_outage(monkeypatch):
    # pypi.org down -> _project_metadata marks metadata unavailable, so the
    # endpoint still responds 200 and lets policy-sync fail open to an un-stamped
    # base list without caching that degraded response.
    customizer = make_stage("P3D")

    def boom(project, pypi_json_url, now=time.time):
        raise MetadataUnavailable("pypi down")
    monkeypatch.setattr(main, "fetch_project_metadata", boom)

    resp = project_meta_view(FakeRequest(
        "/+artea/project-meta", registry=_meta_registry(customizer), params={"name": "six"}))
    assert resp.status_code == 200
    assert json.loads(resp.body) == {"file_meta": {}, "metadata_available": False}


def test_project_meta_endpoint_rejects_missing_name():
    customizer = make_stage("P0D")
    with pytest.raises(HTTPBadRequest):
        project_meta_view(FakeRequest(
            "/+artea/project-meta", registry=_meta_registry(customizer), params={}))
