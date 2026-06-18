import dataclasses
import json

import pytest

from policy_sync.devpi import DevpiError, apply_constraints
from tests.conftest import TEST_DEVPI_PASSWORD

CONSTRAINTS = "urllib3<2\n# pinned for CVE-XXXX\nrequests ==2.31.0\n"


def test_apply_get_then_patch_with_root_auth(cfg, mock_devpi):
    apply_constraints(cfg, CONSTRAINTS, "P3D")
    assert [r["method"] for r in mock_devpi.requests] == ["GET", "PATCH"]
    assert all(r["path"] == "/root/constrained" for r in mock_devpi.requests)
    patch = mock_devpi.patches[0]
    assert patch["authorization"].startswith("Basic ")
    assert mock_devpi.config["constraints"] == CONSTRAINTS
    assert mock_devpi.config["min_upstream_age"] == "P3D"


def test_apply_preserves_other_index_config_keys(cfg, mock_devpi):
    # the PATCH body must be the full config dict with only constraints replaced
    apply_constraints(cfg, CONSTRAINTS, "P3D")
    body = json.loads(mock_devpi.patches[0]["body"])
    assert body["type"] == "constrained"
    assert body["bases"] == ["root/pypi"]
    assert body["constraints"] == CONSTRAINTS
    assert body["min_upstream_age"] == "P3D"


def test_apply_skips_patch_when_devpi_already_matches(cfg, mock_devpi):
    # real devpi returns constraints normalized: a list without comments/blanks
    mock_devpi.config["constraints"] = ["urllib3<2", "requests ==2.31.0"]
    mock_devpi.config["min_upstream_age"] = "P3D"
    assert apply_constraints(cfg, CONSTRAINTS, "P3D") is False
    assert mock_devpi.patches == []


def test_apply_patches_when_devpi_differs(cfg, mock_devpi):
    mock_devpi.config["constraints"] = ["*"]  # entrypoint's fail-closed seed
    assert apply_constraints(cfg, CONSTRAINTS, "P0D") is True
    assert mock_devpi.config["constraints"] == CONSTRAINTS


def test_apply_rejects_wrong_index_type(cfg, mock_devpi):
    mock_devpi.config["type"] = "mirror"
    with pytest.raises(DevpiError, match="expected 'constrained'"):
        apply_constraints(cfg, CONSTRAINTS, "P0D")
    assert mock_devpi.patches == []


def test_apply_rejects_wrong_index_base(cfg, mock_devpi):
    mock_devpi.config["bases"] = ["root/other"]
    with pytest.raises(DevpiError, match="expected root/pypi"):
        apply_constraints(cfg, CONSTRAINTS, "P0D")
    assert mock_devpi.patches == []


def test_apply_patches_when_min_upstream_age_differs(cfg, mock_devpi):
    mock_devpi.config["constraints"] = ["urllib3<2", "requests ==2.31.0"]
    mock_devpi.config["min_upstream_age"] = "P0D"
    assert apply_constraints(cfg, CONSTRAINTS, "P3D") is True
    assert mock_devpi.config["min_upstream_age"] == "P3D"


def test_get_failure_raises_without_patching(cfg, mock_devpi):
    mock_devpi.fail_remaining = 1
    with pytest.raises(DevpiError, match="GET .* 500"):
        apply_constraints(cfg, CONSTRAINTS, "P0D")
    assert mock_devpi.patches == []


def test_patch_failure_raises_with_password_redacted(cfg, mock_devpi):
    mock_devpi.config = {"type": "constrained", "bases": ["root/pypi"]}
    bad_cfg = dataclasses.replace(cfg, devpi_root_password="wrong-pass")
    with pytest.raises(DevpiError) as exc:
        apply_constraints(bad_cfg, CONSTRAINTS, "P0D")
    assert "403" in str(exc.value)
    assert "wrong-pass" not in str(exc.value)
    assert TEST_DEVPI_PASSWORD not in str(exc.value)


def test_connection_refused_raises_devpi_error(cfg, mock_devpi):
    mock_devpi.stop()
    with pytest.raises(DevpiError, match="failed"):
        apply_constraints(cfg, CONSTRAINTS, "P0D")
