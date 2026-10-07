"""One teleharvester process per folder (modules/instance_lock.py)."""

import os

import pytest

from modules import instance_lock


@pytest.fixture
def lock_path(monkeypatch, tmp_path):
    path = tmp_path / "tmp" / "teleharvester.lock"
    monkeypatch.setattr(instance_lock, "LOCK_PATH", str(path))
    monkeypatch.setattr(instance_lock, "_held", None)
    yield path
    if instance_lock._held is not None:
        os.close(instance_lock._held)  # drops the lock


def test_second_process_is_refused(lock_path):
    instance_lock.hold()
    first = instance_lock._held

    with pytest.raises(SystemExit, match="уже запущен"):
        instance_lock.hold()  # a second open file of the same lock: as another process would
    assert instance_lock._held == first and lock_path.exists()


def test_lock_is_free_again_once_its_holder_is_gone(lock_path):
    instance_lock.hold()
    os.close(instance_lock._held)  # the OS drops a flock when its process exits

    instance_lock.hold()
    assert instance_lock._held is not None


def test_a_lock_file_left_by_another_user_does_not_stop_the_start(lock_path):
    # e.g. `sudo python main.py` once: the file is root's, read-only for the bot's user
    lock_path.parent.mkdir()
    lock_path.touch()
    lock_path.chmod(0o444)

    instance_lock.hold()
    assert instance_lock._held is not None


def test_cli_takes_the_lock_before_the_update_check(monkeypatch):
    # a `git pull` + `pip install` must not run under a live bot: the lock refuses first
    import main

    checked = []

    def refuse():
        raise SystemExit("teleharvester уже запущен")

    monkeypatch.setattr(main.instance_lock, "hold", refuse)
    monkeypatch.setattr(main.updater, "check_update", lambda: checked.append(1) or {"has_update": False})

    with pytest.raises(SystemExit, match="уже запущен"):
        main.main()
    assert checked == []
