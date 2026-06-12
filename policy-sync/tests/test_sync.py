from dataclasses import replace
from pathlib import Path

from policy_sync.store import PolicyStore
from policy_sync.sync import Syncer

NPM = b"blocked:\n  packages: []\n"
PYPI = b"# constraints\nurllib3<2\n"


def make_syncer(cfg, store=None):
    sleeps = []
    syncer = Syncer(cfg, sleep=sleeps.append, store=store)
    return syncer, sleeps


def test_full_sync_writes_npm_and_applies_constraints(cfg, mock_gitea, mock_devpi):
    mock_gitea.files["npm-rules.yaml"] = NPM
    mock_gitea.files["pypi-constraints.txt"] = PYPI
    syncer, _ = make_syncer(cfg)

    assert syncer.sync_once() is True
    assert (Path(cfg.policy_file_path)).read_bytes() == NPM
    assert mock_devpi.config["constraints"] == PYPI.decode()


def test_resync_unchanged_is_idempotent(cfg, mock_gitea, mock_devpi):
    mock_gitea.files["npm-rules.yaml"] = NPM
    mock_gitea.files["pypi-constraints.txt"] = PYPI
    syncer, _ = make_syncer(cfg)
    syncer.sync_once()
    npm_path = Path(cfg.policy_file_path)
    mtime = npm_path.stat().st_mtime_ns
    patches = len(mock_devpi.patches)

    assert syncer.sync_once() is True
    assert npm_path.stat().st_mtime_ns == mtime  # no spurious mtime bump
    assert len(mock_devpi.patches) == patches  # no devpi churn


def test_wiped_devpi_healed_on_next_sync_with_unchanged_policy(cfg, mock_gitea, mock_devpi):
    # a recreated devpi index carries the entrypoint's fail-closed '*' seed;
    # the next sync must replace it even though the policy file is unchanged
    mock_gitea.files["npm-rules.yaml"] = NPM
    mock_gitea.files["pypi-constraints.txt"] = PYPI
    syncer, _ = make_syncer(cfg)
    syncer.sync_once()

    mock_devpi.config["constraints"] = ["*"]  # simulate wipe + fail-closed seed
    assert syncer.sync_once() is True
    assert mock_devpi.config["constraints"] == PYPI.decode()


def test_changed_constraints_reapplied(cfg, mock_gitea, mock_devpi):
    mock_gitea.files["npm-rules.yaml"] = NPM
    mock_gitea.files["pypi-constraints.txt"] = PYPI
    syncer, _ = make_syncer(cfg)
    syncer.sync_once()

    mock_gitea.files["pypi-constraints.txt"] = b"urllib3<2\nrequests ==2.31.0\n"
    assert syncer.sync_once() is True
    assert mock_devpi.config["constraints"] == "urllib3<2\nrequests ==2.31.0\n"


def test_missing_file_skipped_without_failing(cfg, mock_gitea, mock_devpi):
    mock_gitea.files["pypi-constraints.txt"] = PYPI  # npm-rules.yaml absent
    syncer, _ = make_syncer(cfg)

    assert syncer.sync_once() is True
    assert not (Path(cfg.policy_file_path)).exists()
    assert mock_devpi.config["constraints"] == PYPI.decode()


def test_gitea_down_retries_with_backoff_then_gives_up(cfg, mock_gitea):
    mock_gitea.files["npm-rules.yaml"] = NPM
    mock_gitea.files["pypi-constraints.txt"] = PYPI
    mock_gitea.fail_remaining = 100
    syncer, sleeps = make_syncer(cfg)

    assert syncer.sync_with_retry(attempts=3, base_delay=2) is False
    assert sleeps == [2, 4]  # exponential, attempts-1 sleeps, never raises


def test_recovers_after_transient_failure(cfg, mock_gitea):
    mock_gitea.files["npm-rules.yaml"] = NPM
    mock_gitea.files["pypi-constraints.txt"] = PYPI
    mock_gitea.fail_remaining = 2  # first attempt fails both fetches
    syncer, sleeps = make_syncer(cfg)

    assert syncer.sync_with_retry(attempts=3) is True
    assert len(sleeps) == 1
    assert (Path(cfg.policy_file_path)).read_bytes() == NPM


def test_devpi_failure_marks_sync_failed_but_npm_still_written(cfg, mock_gitea, mock_devpi):
    mock_gitea.files["npm-rules.yaml"] = NPM
    mock_gitea.files["pypi-constraints.txt"] = PYPI
    mock_devpi.fail_remaining = 100
    syncer, _ = make_syncer(cfg)

    assert syncer.sync_once() is False
    assert (Path(cfg.policy_file_path)).read_bytes() == NPM


def test_http_only_mode_updates_store_and_writes_no_file(cfg_http_only, mock_gitea, mock_devpi, tmp_path):
    mock_gitea.files["npm-rules.yaml"] = NPM
    mock_gitea.files["pypi-constraints.txt"] = PYPI
    store = PolicyStore()
    syncer, _ = make_syncer(cfg_http_only, store=store)

    assert syncer.sync_once() is True
    content, etag = store.get()
    assert content == NPM
    assert etag.startswith('"') and etag.endswith('"')
    assert list(tmp_path.iterdir()) == []  # no file write anywhere


def test_file_and_http_modes_serve_identical_bytes(cfg, mock_gitea, mock_devpi):
    # parity: in file+HTTP mode the endpoint and the volume file never diverge
    mock_gitea.files["npm-rules.yaml"] = NPM
    mock_gitea.files["pypi-constraints.txt"] = PYPI
    store = PolicyStore(fallback_path=cfg.policy_file_path)
    syncer, _ = make_syncer(cfg, store=store)

    assert syncer.sync_once() is True
    content, _ = store.get()
    assert content == Path(cfg.policy_file_path).read_bytes() == NPM

    mock_gitea.files["npm-rules.yaml"] = b"blocked:\n  packages:\n    - left-pad\n"
    assert syncer.sync_once() is True
    content, _ = store.get()
    assert content == Path(cfg.policy_file_path).read_bytes() != NPM


def test_missing_parent_directory_is_created(cfg, mock_gitea, mock_devpi, tmp_path):
    # POLICY_FILE_PATH may point at a private tmp dir that does not exist yet
    cfg = replace(cfg, policy_file_path=str(tmp_path / "private" / "npm-rules.yaml"))
    mock_gitea.files["npm-rules.yaml"] = NPM
    syncer, _ = make_syncer(cfg)

    assert syncer.sync_once() is True
    assert Path(cfg.policy_file_path).read_bytes() == NPM
