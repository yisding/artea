"""Operational behavior of one sync (retry, idempotency, fail-closed,
file/HTTP parity) — format-agnostic, exercised through the unified policy.toml.
The compilation contract itself lives in test_compiler.py / test_sync_unified.py.
"""

from dataclasses import replace
from pathlib import Path

from policy_sync.store import PolicyStore
from tests.conftest import UNIFIED, make_syncer


def test_full_sync_writes_artifacts_and_applies_constraints(cfg, mock_gitea, mock_devpi):
    mock_gitea.files["policy.toml"] = UNIFIED
    syncer, _ = make_syncer(cfg)

    assert syncer.sync_once() is True
    assert b'"event-stream"' in Path(cfg.policy_file_path).read_bytes()
    assert Path(cfg.upstream_policy_file_path).read_bytes() == b'upstream:\n  min_age: "P3D"\n'
    assert Path(cfg.pypi_policy_file_path).read_text() == "urllib3<2\n"
    assert mock_devpi.config["constraints"] == "urllib3<2\n"
    assert mock_devpi.config["min_upstream_age"] == "P3D"


def test_resync_unchanged_is_idempotent(cfg, mock_gitea, mock_devpi):
    mock_gitea.files["policy.toml"] = UNIFIED
    syncer, _ = make_syncer(cfg)
    syncer.sync_once()
    npm_path = Path(cfg.policy_file_path)
    pypi_path = Path(cfg.pypi_policy_file_path)
    upstream_path = Path(cfg.upstream_policy_file_path)
    mtime = npm_path.stat().st_mtime_ns
    pypi_mtime = pypi_path.stat().st_mtime_ns
    upstream_mtime = upstream_path.stat().st_mtime_ns
    patches = len(mock_devpi.patches)

    assert syncer.sync_once() is True
    assert npm_path.stat().st_mtime_ns == mtime  # byte-stable emit, no mtime bump
    assert pypi_path.stat().st_mtime_ns == pypi_mtime
    assert upstream_path.stat().st_mtime_ns == upstream_mtime
    assert len(mock_devpi.patches) == patches  # no devpi churn


def test_wiped_devpi_healed_on_next_sync_with_unchanged_policy(cfg, mock_gitea, mock_devpi):
    # a recreated devpi index carries the entrypoint's fail-closed '*' seed;
    # the next sync must replace it even though the policy is unchanged
    mock_gitea.files["policy.toml"] = UNIFIED
    syncer, _ = make_syncer(cfg)
    syncer.sync_once()

    mock_devpi.config["constraints"] = ["*"]  # simulate wipe + fail-closed seed
    assert syncer.sync_once() is True
    assert mock_devpi.config["constraints"] == "urllib3<2\n"
    assert mock_devpi.config["min_upstream_age"] == "P3D"


def test_changed_constraints_reapplied(cfg, mock_gitea, mock_devpi):
    mock_gitea.files["policy.toml"] = UNIFIED
    syncer, _ = make_syncer(cfg)
    syncer.sync_once()

    mock_gitea.files["policy.toml"] = UNIFIED.replace(b'versions = ">=2"', b'versions = ">=3"')
    assert syncer.sync_once() is True
    assert mock_devpi.config["constraints"] == "urllib3<3\n"


def test_changed_min_age_reapplied_to_devpi(cfg, mock_gitea, mock_devpi):
    mock_gitea.files["policy.toml"] = UNIFIED
    syncer, _ = make_syncer(cfg)
    syncer.sync_once()

    mock_gitea.files["policy.toml"] = UNIFIED.replace(b'min_age = "P3D"', b'min_age = "PT12H"')
    assert syncer.sync_once() is True
    assert mock_devpi.config["min_upstream_age"] == "PT12H"


def test_absent_policy_fails_keeping_last_known_good(cfg, mock_gitea, mock_devpi):
    mock_gitea.files["policy.toml"] = UNIFIED
    syncer, _ = make_syncer(cfg)
    assert syncer.sync_once() is True
    good_npm = Path(cfg.policy_file_path).read_bytes()
    patches_before = len(mock_devpi.patches)

    # policy.toml disappears from the repo: keep last-known-good, fail the sync
    # (so /healthz reports last_sync_ok=false) — never tear enforcement down.
    del mock_gitea.files["policy.toml"]
    assert syncer.sync_once() is False
    assert Path(cfg.policy_file_path).read_bytes() == good_npm
    assert len(mock_devpi.patches) == patches_before  # devpi untouched


def test_gitea_down_retries_with_backoff_then_gives_up(cfg, mock_gitea):
    mock_gitea.files["policy.toml"] = UNIFIED
    mock_gitea.fail_remaining = 100
    syncer, sleeps = make_syncer(cfg)

    assert syncer.sync_with_retry(attempts=3, base_delay=2) is False
    assert sleeps == [2, 4]  # exponential, attempts-1 sleeps, never raises


def test_recovers_after_transient_failure(cfg, mock_gitea):
    mock_gitea.files["policy.toml"] = UNIFIED
    mock_gitea.fail_remaining = 1  # first policy.toml fetch 500s, then recovers
    syncer, sleeps = make_syncer(cfg)

    assert syncer.sync_with_retry(attempts=3) is True
    assert len(sleeps) == 1
    assert b'"event-stream"' in Path(cfg.policy_file_path).read_bytes()


def test_devpi_failure_marks_sync_failed_but_npm_still_written(cfg, mock_gitea, mock_devpi):
    mock_gitea.files["policy.toml"] = UNIFIED
    mock_devpi.fail_remaining = 100
    syncer, _ = make_syncer(cfg)

    assert syncer.sync_once() is False
    # npm and the pypi debug file are written before the devpi PATCH is attempted
    assert b'"event-stream"' in Path(cfg.policy_file_path).read_bytes()
    assert Path(cfg.pypi_policy_file_path).read_text() == "urllib3<2\n"


def test_http_only_mode_updates_store_and_writes_no_file(cfg_http_only, mock_gitea, mock_devpi, tmp_path):
    mock_gitea.files["policy.toml"] = UNIFIED
    store = PolicyStore()
    upstream_store = PolicyStore()
    syncer, _ = make_syncer(cfg_http_only, store=store, upstream_store=upstream_store)

    assert syncer.sync_once() is True
    content, etag = store.get()
    assert b'"event-stream"' in content
    assert etag.startswith('"') and etag.endswith('"')
    upstream_content, upstream_etag = upstream_store.get()
    assert upstream_content == b'upstream:\n  min_age: "P3D"\n'
    assert upstream_etag.startswith('"') and upstream_etag.endswith('"')
    assert list(tmp_path.iterdir()) == []  # no file write anywhere


def test_file_and_http_modes_serve_identical_bytes(cfg, mock_gitea, mock_devpi):
    # parity: in file+HTTP mode the endpoint and the volume file never diverge
    mock_gitea.files["policy.toml"] = UNIFIED
    store = PolicyStore(fallback_path=cfg.policy_file_path)
    upstream_store = PolicyStore(fallback_path=cfg.upstream_policy_file_path)
    syncer, _ = make_syncer(cfg, store=store, upstream_store=upstream_store)

    assert syncer.sync_once() is True
    content, _ = store.get()
    assert content == Path(cfg.policy_file_path).read_bytes()
    assert b'"event-stream"' in content
    upstream_content, _ = upstream_store.get()
    assert upstream_content == Path(cfg.upstream_policy_file_path).read_bytes()


def test_missing_parent_directory_is_created(cfg, mock_gitea, mock_devpi, tmp_path):
    # POLICY_FILE_PATH may point at a private tmp dir that does not exist yet
    cfg = replace(cfg, policy_file_path=str(tmp_path / "private" / "npm-rules.yaml"))
    mock_gitea.files["policy.toml"] = UNIFIED
    syncer, _ = make_syncer(cfg)

    assert syncer.sync_once() is True
    assert b'"event-stream"' in Path(cfg.policy_file_path).read_bytes()
