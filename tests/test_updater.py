"""Updater: never touches a non-git install dir; pip --user only outside a venv."""

import contextlib
import sys

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
