from pathlib import Path

from policy_sync.store import PolicyStore
from policy_sync.sync import Syncer

UNIFIED = b"""
schema = 1
[upstream]
min_age = "P3D"
[[rules]]
ecosystem = "npm"
name = "event-stream"
action = "deny"
[[rules]]
ecosystem = "pypi"
name = "urllib3"
versions = ">=2"
action = "deny"
"""


def make_syncer(cfg, store=None, upstream_store=None):
    sleeps = []
    syncer = Syncer(cfg, sleep=sleeps.append, store=store, upstream_store=upstream_store)
    return syncer, sleeps


def _requested_paths(mock_gitea):
    return [r["path"] for r in mock_gitea.requests]


def test_unified_present_compiles_and_applies(cfg, mock_gitea, mock_devpi):
    mock_gitea.files["policy.toml"] = UNIFIED
    syncer, _ = make_syncer(cfg)

    assert syncer.sync_once() is True

    npm = Path(cfg.policy_file_path).read_text()
    assert '- "event-stream"' in npm
    assert '  min_age: "P3D"' in npm
    assert mock_devpi.config["constraints"] == "urllib3<2\n"
    assert mock_devpi.config["min_upstream_age"] == "P3D"

    # the Verdaccio CompositePolicyLoader takes min_age solely from
    # upstream-policy.yaml; the unified path must emit it (else npm fails closed).
    upstream = Path(cfg.upstream_policy_file_path).read_text()
    assert upstream == 'upstream:\n  min_age: "P3D"\n'

    # only policy.toml is fetched — no stray artifact-name probes
    paths = _requested_paths(mock_gitea)
    assert not any("npm-rules.yaml" in p for p in paths)
    assert not any("pypi-constraints.txt" in p for p in paths)
    assert not any("upstream-policy.yaml" in p for p in paths)


def test_bad_unified_keeps_last_known_good(cfg, mock_gitea, mock_devpi):
    # first sync: good policy applied
    mock_gitea.files["policy.toml"] = UNIFIED
    syncer, _ = make_syncer(cfg)
    assert syncer.sync_once() is True
    good_npm = Path(cfg.policy_file_path).read_bytes()
    good_constraints = mock_devpi.config["constraints"]
    patches_before = len(mock_devpi.patches)

    # now break the policy structurally
    mock_gitea.files["policy.toml"] = b'schema = 1\n[[rules]]\necosystem = "npm"\n'
    assert syncer.sync_once() is False

    # last-known-good preserved: npm file unchanged, devpi untouched
    assert Path(cfg.policy_file_path).read_bytes() == good_npm
    assert mock_devpi.config["constraints"] == good_constraints
    assert len(mock_devpi.patches) == patches_before  # no PATCH on the bad sync


def test_unified_exact_allow_end_to_end(cfg, mock_gitea, mock_devpi):
    mock_gitea.files["policy.toml"] = b"""
schema = 1
[[rules]]
ecosystem = "npm"
name = "p"
action = "deny"
[[rules]]
ecosystem = "npm"
name = "p"
versions = "1.2.3"
action = "allow"
[[rules]]
ecosystem = "pypi"
name = "q"
action = "deny"
[[rules]]
ecosystem = "pypi"
name = "q"
versions = "==1.2.3"
action = "allow"
"""
    syncer, _ = make_syncer(cfg)
    assert syncer.sync_once() is True
    assert '"<1.2.3 || >1.2.3"' in Path(cfg.policy_file_path).read_text()
    assert mock_devpi.config["constraints"] == "q==1.2.3\n"


def test_unified_http_only_mode(cfg_http_only, mock_gitea, mock_devpi, tmp_path):
    mock_gitea.files["policy.toml"] = UNIFIED
    store = PolicyStore()
    syncer, _ = make_syncer(cfg_http_only, store=store)

    assert syncer.sync_once() is True
    content, etag = store.get()
    assert b'- "event-stream"' in content
    assert etag.startswith('"')
    assert mock_devpi.config["constraints"] == "urllib3<2\n"
    assert list(tmp_path.iterdir()) == []  # no file written


def test_unified_emits_upstream_to_store(cfg, mock_gitea, mock_devpi):
    mock_gitea.files["policy.toml"] = UNIFIED
    store = PolicyStore()
    upstream_store = PolicyStore()
    syncer, _ = make_syncer(cfg, store=store, upstream_store=upstream_store)

    assert syncer.sync_once() is True
    content, _etag = upstream_store.get()
    assert content == b'upstream:\n  min_age: "P3D"\n'


def test_unified_http_only_emits_upstream_to_store_no_file(
    cfg_http_only, mock_gitea, mock_devpi, tmp_path
):
    mock_gitea.files["policy.toml"] = UNIFIED
    store = PolicyStore()
    upstream_store = PolicyStore()
    syncer, _ = make_syncer(cfg_http_only, store=store, upstream_store=upstream_store)

    assert syncer.sync_once() is True
    content, _etag = upstream_store.get()
    assert content == b'upstream:\n  min_age: "P3D"\n'
    assert list(tmp_path.iterdir()) == []  # HTTP-only: no upstream file written


def test_unified_min_age_applied_to_devpi(cfg, mock_gitea, mock_devpi):
    mock_gitea.files["policy.toml"] = UNIFIED
    syncer, _ = make_syncer(cfg)
    assert syncer.sync_once() is True
    assert mock_devpi.config["min_upstream_age"] == "P3D"


def test_unified_resync_idempotent(cfg, mock_gitea, mock_devpi):
    mock_gitea.files["policy.toml"] = UNIFIED
    syncer, _ = make_syncer(cfg)
    syncer.sync_once()
    npm_path = Path(cfg.policy_file_path)
    mtime = npm_path.stat().st_mtime_ns
    patches = len(mock_devpi.patches)

    assert syncer.sync_once() is True
    assert npm_path.stat().st_mtime_ns == mtime  # byte-stable emit, no mtime bump
    assert len(mock_devpi.patches) == patches  # no devpi churn
