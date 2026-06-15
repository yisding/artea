import sys
import time
import tomllib
from pathlib import Path

import pytest

PLUGIN_SRC = Path(__file__).resolve().parents[1] / "artea_devpi_policy" / "src"
sys.path.insert(0, str(PLUGIN_SRC))

from artea_devpi_policy.main import (  # noqa: E402
    ConstrainedStage,
    ProjectMetadata,
    file_age_tween_factory,
    pypi_file_allowed_view,
    parse_iso_duration_seconds,
)
from pyramid.httpexceptions import HTTPForbidden  # noqa: E402


class FakeStage:
    def __init__(self, ixconfig):
        self.ixconfig = ixconfig


class FakeLink:
    def __init__(self, version, filename):
        self.name = "six"
        self.project = "six"
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


def test_file_allowed_requires_upload_time(monkeypatch):
    now = time.time()
    customizer = make_stage("P3D")
    metadata = ProjectMetadata(
        fetched_at=now,
        files={"six-1.0.0-py3-none-any.whl": now - 4 * 24 * 60 * 60},
        versions={},
    )
    monkeypatch.setattr(customizer, "_project_metadata", lambda project: metadata)

    assert customizer.file_allowed("six", "six-1.0.0-py3-none-any.whl") is True
    assert customizer.file_allowed("six", "six-2.0.0-py3-none-any.whl") is False


def test_link_allowed_applies_constraints_without_rendering_simple_page():
    customizer = make_stage("P0D")
    customizer.stage.ixconfig["constraints"] = ["six<2"]

    assert customizer.link_allowed("six", FakeLink("1.17.0", "six-1.17.0-py2.py3-none-any.whl")) is True
    assert customizer.link_allowed("six", FakeLink("2.0.0", "six-2.0.0-py3-none-any.whl")) is False
    assert customizer.link_allowed("six", FakeELink("1.17.0", "six-1.17.0-py2.py3-none-any.whl")) is True


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
