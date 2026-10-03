"""Offline tests for credential loading and session selection."""

import pytest
from telethon.crypto import AuthKey
from telethon.sessions import StringSession

from scraper import config
from scraper.config import Credentials, load_credentials, session_for, start_kwargs


def test_load_credentials_reads_session_string(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)  # keep a real .env out of the test
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", "h")
    monkeypatch.setenv("TG_SESSION_STRING", "abc")
    assert load_credentials().session_string == "abc"


def test_session_for_prefers_string_over_file():
    assert session_for(Credentials(1, "h"), "file") == "file"
    saved = StringSession()
    saved.set_dc(2, "149.154.167.51", 443)
    saved.auth_key = AuthKey(bytes(256))  # save() is "" without a key
    s = session_for(Credentials(1, "h", session_string=saved.save()), "file")
    assert isinstance(s, StringSession) and s.dc_id == 2


@pytest.mark.parametrize("bad", ["abc", "1abc"])  # not a string / truncated paste
def test_session_for_rejects_malformed_string(bad):
    with pytest.raises(SystemExit, match="TG_SESSION_STRING"):
        session_for(Credentials(1, "h", session_string=bad), "file")


def test_login_without_username_prints_no_at_none(monkeypatch, capsys):
    import types

    from scraper import login

    class FakeClient:
        session = "file"

        def __init__(self, *a, **k):
            pass

        async def start(self, **k):
            return self

        async def get_me(self):
            return types.SimpleNamespace(first_name="Ann", username=None, id=1)

        async def disconnect(self):
            return None

    monkeypatch.setattr(login, "TelegramClient", FakeClient)
    login.login(Credentials(1, "h"), "file")
    out = capsys.readouterr().out
    assert "Logged in as Ann, id 1" in out and "Session saved: file.session\n" in out
    login.login(Credentials(1, "h"), "file.session")  # Telethon doesn't add a second suffix
    assert "Session saved: file.session\n" in capsys.readouterr().out


def test_load_credentials_reads_dotenv_from_cwd(monkeypatch, tmp_path):
    for var in ("TG_API_ID", "TG_API_HASH", "TG_SESSION_STRING"):
        monkeypatch.setenv(var, "x")  # recorded, so teardown removes what dotenv sets
        monkeypatch.delenv(var)
    (tmp_path / ".env").write_text("TG_API_ID=42\nTG_API_HASH=abc\n")
    monkeypatch.chdir(tmp_path)
    assert load_credentials().api_id == 42  # not the .env next to the package


def test_load_credentials_ignores_config_toml(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("TG_API_ID", raising=False)
    monkeypatch.delenv("TG_API_HASH", raising=False)
    (tmp_path / "config.toml").write_text('[sessions]\napi_id = 42\napi_hash = "abc"\n')
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="TG_API_ID"):
        load_credentials()


def test_start_kwargs_leaves_unset_phone_and_password_to_telethon():
    import asyncio

    from telethon import TelegramClient

    assert start_kwargs(Credentials(1, "h")) == {}
    assert start_kwargs(Credentials(1, "h", phone="+1", password="p")) == {"phone": "+1", "password": "p"}

    async def check():  # inside a running loop start() only builds the coroutine: no network
        client = TelegramClient(StringSession(), 1, "h")
        with pytest.raises(ValueError, match="No phone number"):  # what we used to pass
            client.start(phone=None, password=None)
        client.start(**start_kwargs(Credentials(1, "h"))).close()  # falls back to its prompts

    asyncio.run(check())
