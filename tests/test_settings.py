"""Settings: secrets come from the environment (.env), behaviour from config.toml."""

import pytest

from modules.settings import Settings

BROADCAST = '[broadcast]\nmessages = ["hi"]\ndelay = [1, 2]\nmessages_count = 0\ntrigger = "go"\n'


@pytest.fixture
def cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_reads_api_keys_from_env(monkeypatch, cwd):
    (cwd / "config.toml").write_text(BROADCAST)
    monkeypatch.setenv("TG_API_ID", "42")
    monkeypatch.setenv("TG_API_HASH", "abc")
    settings = Settings()
    assert (settings.api_id, settings.api_hash) == (42, "abc")
    assert settings.messages == ["hi"] and settings.delay == [1, 2]


@pytest.mark.parametrize("api_id, api_hash", [("", "abc"), ("42", ""), ("x42", "abc")])
def test_missing_or_bad_env_exits(monkeypatch, cwd, api_id, api_hash):
    (cwd / "config.toml").write_text(BROADCAST)
    monkeypatch.setenv("TG_API_ID", api_id)
    monkeypatch.setenv("TG_API_HASH", api_hash)
    with pytest.raises(SystemExit):
        Settings()


def test_legacy_sessions_section_is_rejected(monkeypatch, cwd):
    (cwd / "config.toml").write_text('[sessions]\napi_id = 42\napi_hash = "abc"\n\n' + BROADCAST)
    monkeypatch.setenv("TG_API_ID", "42")
    monkeypatch.setenv("TG_API_HASH", "abc")
    with pytest.raises(SystemExit):
        Settings()


def test_initial_setup_writes_keys_to_env_not_toml(monkeypatch, cwd):
    monkeypatch.delenv("TG_API_ID", raising=False)
    monkeypatch.delenv("TG_API_HASH", raising=False)
    monkeypatch.setattr(Settings, "setup_sessions", staticmethod(lambda: (42, "abc")))
    monkeypatch.setattr(Settings, "setup_broadcast", staticmethod(lambda: (["hi"], [1, 2], "go")))
    Settings.initial_setup()
    env = (cwd / ".env").read_text()
    assert "TG_API_ID='42'" in env and "TG_API_HASH='abc'" in env
    assert "sessions" not in (cwd / "config.toml").read_text()


def test_setup_sessions_reasks_non_numeric_api_id(monkeypatch):
    answers = iter(["abc", "42", "hash"])
    monkeypatch.setattr("modules.settings.console.input", lambda *_: next(answers))
    assert Settings.setup_sessions() == (42, "hash")
