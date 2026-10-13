"""Updater: never touches a non-git install dir; pip --user only outside a venv."""

import contextlib
import sys
import types

from modules import updater


def test_check_update_skips_non_git_dir(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    assert updater.check_update() == {"has_update": False, "checked": False}
    assert not (tmp_path / ".git").exists()  # no git init / force checkout over local files
    assert updater.NOT_A_CHECKOUT in capsys.readouterr().out


class _Console:
    def status(self, _):
        return contextlib.nullcontext()

    def print(self, *_):
        pass


def _pip_args(monkeypatch, base_prefix):
    calls = []
    monkeypatch.setattr(updater.subprocess, "run", lambda args, **_: calls.append(args))
    monkeypatch.setattr(sys, "base_prefix", base_prefix)
    updater.update_requirements(_Console())
    return calls[0]


def test_update_requirements_no_user_flag_in_venv(monkeypatch):
    assert "--user" not in _pip_args(monkeypatch, sys.prefix + "-base")


def test_update_requirements_user_flag_outside_venv(monkeypatch):
    assert "--user" in _pip_args(monkeypatch, sys.prefix)


def test_dubious_ownership_is_fixed_once_not_in_a_loop(monkeypatch):
    from git.exc import GitCommandError

    def fetch(**kwargs):
        raise GitCommandError(["git", "fetch"], 128, "fatal: detected dubious ownership in repository")

    calls = []
    monkeypatch.setattr(updater.git, "Repo", lambda *a: object())
    monkeypatch.setattr(updater.git, "Remote", lambda repo, name: types.SimpleNamespace(fetch=fetch))
    monkeypatch.setattr(updater.subprocess, "run", lambda args, **_: calls.append(args))

    assert updater.check_update() == {"has_update": False, "checked": False}
    assert len(calls) == 1  # each retry would add another safe.directory line to ~/.gitconfig


def test_an_unreachable_remote_does_not_hold_the_start(monkeypatch, capsys):
    import threading
    import time

    hung = threading.Event()

    def fetch(**kwargs):  # ssh to an unreachable remote: git's output stays open for minutes
        hung.wait(5)

    monkeypatch.setattr(updater, "FETCH_TIMEOUT", 0.1)
    monkeypatch.setattr(updater.git, "Repo", lambda *a: object())
    monkeypatch.setattr(updater.git, "Remote", lambda repo, name: types.SimpleNamespace(fetch=fetch))

    started = time.monotonic()
    assert updater.check_update() == {"has_update": False, "checked": False}
    hung.set()
    assert time.monotonic() - started < 2  # the start goes on without the check
    assert "не удалось проверить обновления" in capsys.readouterr().out


def test_a_failed_check_is_not_reported_as_up_to_date(monkeypatch):
    import main

    printed = []
    monkeypatch.setattr(main.instance_lock, "hold", lambda: None)
    monkeypatch.setattr(main.updater, "check_update", lambda: {"has_update": False, "checked": False})
    monkeypatch.setattr(main.console, "print", lambda text="", *a, **k: printed.append(str(text)))
    monkeypatch.setattr(main.sys, "version_info", (3, 10, 0))  # stops main() right after the check
    main.main()
    assert not any("последняя версия" in line for line in printed)
