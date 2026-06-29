"""ParsedPolicyStore fallback behavior: an in-memory last-known-good policy with
an on-disk fallback (the last successfully-synced policy.toml) so inline OSV
malicious-package decisions fail CLOSED across a policy-sync restart, before the
first post-restart sync from Gitea repopulates memory.
"""

import logging

from policy_sync.policy_model import parse_policy
from policy_sync.store import ParsedPolicyStore
from tests.conftest import UNIFIED


def test_get_returns_none_without_inmemory_or_fallback():
    assert ParsedPolicyStore().get() is None


def test_get_returns_inmemory_policy_and_ignores_fallback(tmp_path):
    # while memory is populated, a corrupt fallback must never be consulted.
    bad = tmp_path / "policy.toml"
    bad.write_bytes(b"definitely { not ] toml")
    store = ParsedPolicyStore(fallback_path=str(bad))
    policy = parse_policy(UNIFIED)
    store.set(policy)
    assert store.get() is policy


def test_get_falls_back_to_disk_when_memory_empty(tmp_path):
    f = tmp_path / "policy.toml"
    f.write_bytes(UNIFIED)
    store = ParsedPolicyStore(fallback_path=str(f))
    assert store.get() == parse_policy(UNIFIED)


def test_get_returns_none_for_missing_fallback_file(tmp_path):
    # the very first cold start (no good sync yet) has no last-known-good to use.
    store = ParsedPolicyStore(fallback_path=str(tmp_path / "absent.toml"))
    assert store.get() is None


def test_get_returns_none_and_warns_for_corrupt_fallback(tmp_path, caplog):
    bad = tmp_path / "policy.toml"
    bad.write_bytes(b"definitely { not ] toml")
    store = ParsedPolicyStore(fallback_path=str(bad))
    with caplog.at_level(logging.WARNING):
        assert store.get() is None
    assert any("fallback" in r.getMessage() for r in caplog.records)
