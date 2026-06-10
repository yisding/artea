import os

import pytest

from policy_sync import files
from policy_sync.files import write_atomic


def test_write_creates_file(tmp_path):
    dest = tmp_path / "npm-rules.yaml"
    assert write_atomic(dest, b"block: []\n") is True
    assert dest.read_bytes() == b"block: []\n"
    assert (dest.stat().st_mode & 0o777) == 0o644


def test_write_replaces_existing(tmp_path):
    dest = tmp_path / "npm-rules.yaml"
    dest.write_bytes(b"old")
    assert write_atomic(dest, b"new") is True
    assert dest.read_bytes() == b"new"


def test_unchanged_content_skips_write_and_preserves_mtime(tmp_path):
    dest = tmp_path / "npm-rules.yaml"
    write_atomic(dest, b"same")
    before = dest.stat().st_mtime_ns
    assert write_atomic(dest, b"same") is False
    assert dest.stat().st_mtime_ns == before


def test_no_tmp_files_left_behind(tmp_path):
    dest = tmp_path / "npm-rules.yaml"
    write_atomic(dest, b"data")
    assert [p.name for p in tmp_path.iterdir()] == ["npm-rules.yaml"]


def test_tmp_file_in_same_directory_then_renamed(tmp_path, monkeypatch):
    """Atomicity requires tmp + dest on the same filesystem (same dir)."""
    seen = {}
    real_replace = os.replace

    def spy(src, dst):
        seen["src"], seen["dst"] = str(src), str(dst)
        return real_replace(src, dst)

    monkeypatch.setattr(files.os, "replace", spy)
    dest = tmp_path / "npm-rules.yaml"
    write_atomic(dest, b"data")
    assert seen["dst"] == str(dest)
    assert os.path.dirname(seen["src"]) == str(tmp_path)
    assert seen["src"] != seen["dst"]


def test_failed_replace_cleans_up_tmp(tmp_path, monkeypatch):
    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(files.os, "replace", boom)
    dest = tmp_path / "npm-rules.yaml"
    with pytest.raises(OSError):
        write_atomic(dest, b"data")
    assert list(tmp_path.iterdir()) == []
