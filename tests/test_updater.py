"""Updater: never touches a non-git install dir; pip --user only outside a venv."""

import contextlib
import sys
import types

from modules import updater


def test_check_update_skips_non_git_dir(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    assert updater.check_update() == {"has_update": False}
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

    def fetch():
        raise GitCommandError(["git", "fetch"], 128, "fatal: detected dubious ownership in repository")

    calls = []
    monkeypatch.setattr(updater.git, "Repo", lambda *a: object())
    monkeypatch.setattr(updater.git, "Remote", lambda repo, name: types.SimpleNamespace(fetch=fetch))
    monkeypatch.setattr(updater.subprocess, "run", lambda args, **_: calls.append(args))

    assert updater.check_update() == {"has_update": False}
    assert len(calls) == 1  # each retry would add another safe.directory line to ~/.gitconfig
