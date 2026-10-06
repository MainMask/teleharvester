"""modules.json_file: atomic, durable small state files."""

import os

from modules import json_file


def test_roundtrip_and_default(tmp_path):
    path = str(tmp_path / "stats" / "x.json")
    assert json_file.load(path, {"none": True}) == {"none": True}
    json_file.save(path, {"a": 1})
    assert json_file.load(path, {}) == {"a": 1}
    assert not os.path.exists(path + ".tmp")


def test_synced_before_the_rename(tmp_path, monkeypatch):
    events = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (events.append("fsync"), real_fsync(fd)))
    monkeypatch.setattr(os, "replace", lambda a, b: (events.append("replace"), real_replace(a, b)))

    json_file.save(str(tmp_path / "x.json"), {"a": 1})
    assert events == ["fsync", "replace"]
