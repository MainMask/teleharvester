import os

import pytest

from modules import atomic_file


def test_new_file_gets_mode_less_umask(tmp_path):
    path = tmp_path / "secret.txt"
    atomic_file.write(path, "key", mode=0o600)
    assert path.read_text(encoding="utf-8") == "key"
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_rewrite_never_widens_mode(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("old", encoding="utf-8")
    os.chmod(path, 0o640)  # the operator's own mode is kept...
    atomic_file.write(path, "new")
    assert os.stat(path).st_mode & 0o777 == 0o640

    os.chmod(path, 0o644)  # ...and never wider than the caller asks: a session's key stays private
    atomic_file.write(path, "newer", mode=0o600)
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_leftover_temp_file_does_not_lend_its_mode(tmp_path):
    path = tmp_path / "secret.txt"
    leftover = tmp_path / "secret.txt.tmp"
    leftover.write_text("half", encoding="utf-8")
    os.chmod(leftover, 0o666)
    atomic_file.write(path, "key", mode=0o600)
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert not leftover.exists()


def test_failed_write_keeps_old_file(tmp_path, monkeypatch):
    path = tmp_path / "resume.json"
    path.write_text("whole", encoding="utf-8")

    def disk_full(fd):
        raise OSError("disk full")

    monkeypatch.setattr(atomic_file.os, "fsync", disk_full)
    with pytest.raises(OSError):
        atomic_file.write(path, "half")
    assert path.read_text(encoding="utf-8") == "whole"


def test_commit_syncs_before_rename(tmp_path, monkeypatch):
    events = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(atomic_file.os, "fsync", lambda fd: (events.append("fsync"), real_fsync(fd)))
    monkeypatch.setattr(atomic_file.os, "replace", lambda a, b: (events.append("replace"), real_replace(a, b)))
    tmp = tmp_path / "shard.parquet.tmp"
    tmp.write_bytes(b"data")
    atomic_file.commit(tmp, tmp_path / "shard.parquet")
    assert events == ["fsync", "replace"]
    assert (tmp_path / "shard.parquet").read_bytes() == b"data"
