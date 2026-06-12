from dataclasses import replace
from pathlib import Path

from policy_sync.store import PolicyStore
from policy_sync.sync import Syncer

NPM = b"blocked:\n  packages: []\n"
PYPI = b"# constraints\nurllib3<2\n"
UPSTREAM = b"upstream:\n  min_age: P3D\n"


def make_syncer(cfg, store=None, upstream_store=None):
    sleeps = []
    syncer = Syncer(cfg, sleep=sleeps.append, store=store, upstream_store=upstream_store)
    return syncer, sleeps


def test_full_sync_writes_npm_and_applies_constraints(cfg, mock_gitea, mock_devpi):
    mock_gitea.files["npm-rules.yaml"] = NPM
    mock_gitea.files["pypi-constraints.txt"] = PYPI
    mock_gitea.files["upstream-policy.yaml"] = UPSTREAM
    syncer, _ = make_syncer(cfg)

    assert syncer.sync_once() is True
    assert (Path(cfg.policy_file_path)).read_bytes() == NPM
    assert (Path(cfg.pypi_policy_file_path)).read_bytes() == PYPI
    assert (Path(cfg.upstream_policy_file_path)).read_bytes() == UPSTREAM
    assert mock_devpi.config["constraints"] == PYPI.decode()
    assert mock_devpi.config["min_upstream_age"] == "P3D"


def test_resync_unchanged_is_idempotent(cfg, mock_gitea, mock_devpi):
    mock_gitea.files["npm-rules.yaml"] = NPM
    mock_gitea.files["pypi-constraints.txt"] = PYPI
    mock_gitea.files["upstream-policy.yaml"] = UPSTREAM
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
    assert npm_path.stat().st_mtime_ns == mtime  # no spurious mtime bump
    assert pypi_path.stat().st_mtime_ns == pypi_mtime
    assert upstream_path.stat().st_mtime_ns == upstream_mtime
    assert len(mock_devpi.patches) == patches  # no devpi churn


def test_wiped_devpi_healed_on_next_sync_with_unchanged_policy(cfg, mock_gitea, mock_devpi):
    # a recreated devpi index carries the entrypoint's fail-closed '*' seed;
    # the next sync must replace it even though the policy file is unchanged
    mock_gitea.files["npm-rules.yaml"] = NPM
    mock_gitea.files["pypi-constraints.txt"] = PYPI
    mock_gitea.files["upstream-policy.yaml"] = UPSTREAM
    syncer, _ = make_syncer(cfg)
    syncer.sync_once()

    mock_devpi.config["constraints"] = ["*"]  # simulate wipe + fail-closed seed
    assert syncer.sync_once() is True
    assert mock_devpi.config["constraints"] == PYPI.decode()
    assert mock_devpi.config["min_upstream_age"] == "P3D"


def test_changed_constraints_reapplied(cfg, mock_gitea, mock_devpi):
    mock_gitea.files["npm-rules.yaml"] = NPM
    mock_gitea.files["pypi-constraints.txt"] = PYPI
    mock_gitea.files["upstream-policy.yaml"] = UPSTREAM
    syncer, _ = make_syncer(cfg)
    syncer.sync_once()

    mock_gitea.files["pypi-constraints.txt"] = b"urllib3<2\nrequests ==2.31.0\n"
    assert syncer.sync_once() is True
    assert mock_devpi.config["constraints"] == "urllib3<2\nrequests ==2.31.0\n"
    assert mock_devpi.config["min_upstream_age"] == "P3D"


def test_changed_upstream_policy_reapplied_to_devpi(cfg, mock_gitea, mock_devpi):
    mock_gitea.files["npm-rules.yaml"] = NPM
    mock_gitea.files["pypi-constraints.txt"] = PYPI
    mock_gitea.files["upstream-policy.yaml"] = UPSTREAM
    syncer, _ = make_syncer(cfg)
    syncer.sync_once()

    mock_gitea.files["upstream-policy.yaml"] = b"upstream:\n  min_age: PT12H\n"
    assert syncer.sync_once() is True
    assert mock_devpi.config["min_upstream_age"] == "PT12H"


def test_missing_upstream_policy_preserves_last_known_age(cfg, mock_gitea, mock_devpi):
    mock_gitea.files["npm-rules.yaml"] = NPM
    mock_gitea.files["pypi-constraints.txt"] = PYPI
    mock_gitea.files["upstream-policy.yaml"] = UPSTREAM
    syncer, _ = make_syncer(cfg)
    syncer.sync_once()

    del mock_gitea.files["upstream-policy.yaml"]
    assert syncer.sync_once() is True
    assert mock_devpi.config["min_upstream_age"] == "P3D"


def test_missing_pypi_constraints_still_applies_upstream_age(cfg, mock_gitea, mock_devpi):
    mock_gitea.files["npm-rules.yaml"] = NPM
    mock_gitea.files["upstream-policy.yaml"] = UPSTREAM
    mock_devpi.config["constraints"] = ["urllib3<2"]
    syncer, _ = make_syncer(cfg)

    assert syncer.sync_once() is True
    assert mock_devpi.config["constraints"] == ["urllib3<2"]
    assert mock_devpi.config["min_upstream_age"] == "P3D"


def test_startup_uses_upstream_policy_fallback_before_gitea_sync(cfg, mock_gitea, mock_devpi):
    Path(cfg.upstream_policy_file_path).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg.upstream_policy_file_path).write_bytes(UPSTREAM)
    mock_gitea.files["npm-rules.yaml"] = NPM
    mock_gitea.files["pypi-constraints.txt"] = PYPI
    syncer, _ = make_syncer(cfg)

    assert syncer.sync_once() is True
    assert mock_devpi.config["constraints"] == PYPI.decode()
    assert mock_devpi.config["min_upstream_age"] == "P3D"


def test_missing_file_skipped_without_failing(cfg, mock_gitea, mock_devpi):
    mock_gitea.files["pypi-constraints.txt"] = PYPI  # npm-rules.yaml absent
    mock_gitea.files["upstream-policy.yaml"] = UPSTREAM
    syncer, _ = make_syncer(cfg)

    assert syncer.sync_once() is True
    assert not (Path(cfg.policy_file_path)).exists()
    assert Path(cfg.pypi_policy_file_path).read_bytes() == PYPI
    assert mock_devpi.config["constraints"] == PYPI.decode()
    assert mock_devpi.config["min_upstream_age"] == "P3D"


def test_gitea_down_retries_with_backoff_then_gives_up(cfg, mock_gitea):
    mock_gitea.files["npm-rules.yaml"] = NPM
    mock_gitea.files["pypi-constraints.txt"] = PYPI
    mock_gitea.files["upstream-policy.yaml"] = UPSTREAM
    mock_gitea.fail_remaining = 100
    syncer, sleeps = make_syncer(cfg)

    assert syncer.sync_with_retry(attempts=3, base_delay=2) is False
    assert sleeps == [2, 4]  # exponential, attempts-1 sleeps, never raises


def test_recovers_after_transient_failure(cfg, mock_gitea):
    mock_gitea.files["npm-rules.yaml"] = NPM
    mock_gitea.files["pypi-constraints.txt"] = PYPI
    mock_gitea.files["upstream-policy.yaml"] = UPSTREAM
    mock_gitea.fail_remaining = 2  # first attempt fails both fetches
    syncer, sleeps = make_syncer(cfg)

    assert syncer.sync_with_retry(attempts=3) is True
    assert len(sleeps) == 1
    assert (Path(cfg.policy_file_path)).read_bytes() == NPM


def test_devpi_failure_marks_sync_failed_but_npm_still_written(cfg, mock_gitea, mock_devpi):
    mock_gitea.files["npm-rules.yaml"] = NPM
    mock_gitea.files["pypi-constraints.txt"] = PYPI
    mock_gitea.files["upstream-policy.yaml"] = UPSTREAM
    mock_devpi.fail_remaining = 100
    syncer, _ = make_syncer(cfg)

    assert syncer.sync_once() is False
    assert (Path(cfg.policy_file_path)).read_bytes() == NPM
    assert (Path(cfg.pypi_policy_file_path)).read_bytes() == PYPI


def test_http_only_mode_updates_store_and_writes_no_file(cfg_http_only, mock_gitea, mock_devpi, tmp_path):
    mock_gitea.files["npm-rules.yaml"] = NPM
    mock_gitea.files["pypi-constraints.txt"] = PYPI
    mock_gitea.files["upstream-policy.yaml"] = UPSTREAM
    store = PolicyStore()
    upstream_store = PolicyStore()
    syncer, _ = make_syncer(cfg_http_only, store=store, upstream_store=upstream_store)

    assert syncer.sync_once() is True
    content, etag = store.get()
    assert content == NPM
    assert etag.startswith('"') and etag.endswith('"')
    upstream_content, upstream_etag = upstream_store.get()
    assert upstream_content == UPSTREAM
    assert upstream_etag.startswith('"') and upstream_etag.endswith('"')
    assert list(tmp_path.iterdir()) == []  # no file write anywhere


def test_file_and_http_modes_serve_identical_bytes(cfg, mock_gitea, mock_devpi):
    # parity: in file+HTTP mode the endpoint and the volume file never diverge
    mock_gitea.files["npm-rules.yaml"] = NPM
    mock_gitea.files["pypi-constraints.txt"] = PYPI
    mock_gitea.files["upstream-policy.yaml"] = UPSTREAM
    store = PolicyStore(fallback_path=cfg.policy_file_path)
    upstream_store = PolicyStore(fallback_path=cfg.upstream_policy_file_path)
    syncer, _ = make_syncer(cfg, store=store, upstream_store=upstream_store)

    assert syncer.sync_once() is True
    content, _ = store.get()
    assert content == Path(cfg.policy_file_path).read_bytes() == NPM
    upstream_content, _ = upstream_store.get()
    assert upstream_content == Path(cfg.upstream_policy_file_path).read_bytes() == UPSTREAM

    mock_gitea.files["npm-rules.yaml"] = b"blocked:\n  packages:\n    - left-pad\n"
    assert syncer.sync_once() is True
    content, _ = store.get()
    assert content == Path(cfg.policy_file_path).read_bytes() != NPM


def test_missing_parent_directory_is_created(cfg, mock_gitea, mock_devpi, tmp_path):
    # POLICY_FILE_PATH may point at a private tmp dir that does not exist yet
    cfg = replace(cfg, policy_file_path=str(tmp_path / "private" / "npm-rules.yaml"))
    mock_gitea.files["npm-rules.yaml"] = NPM
    mock_gitea.files["upstream-policy.yaml"] = UPSTREAM
    syncer, _ = make_syncer(cfg)

    assert syncer.sync_once() is True
    assert Path(cfg.policy_file_path).read_bytes() == NPM
