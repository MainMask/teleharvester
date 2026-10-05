"""SetPasswordFunc: change 2FA with the stored current password and persist the new one."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass

def _load_func():
    import importlib

    module = importlib.import_module("functions.2fa")
    return module.SetPasswordFunc


@dataclass
class _Me:
    first_name = "Denis"


class _Session:
    def __init__(self):
        self.called_with = None

    async def get_me(self):
        return _Me()

    async def edit_2fa(self, current_password=None, new_password=None):
        self.called_with = (current_password, new_password)


class _Account:
    def __init__(self, password):
        self.password = password


class _Js:
    def __init__(self, password):
        self.account = _Account(password)


class _Storage:
    def __init__(self, path, js):
        self._path = path
        self.jsessions_paths = {path: js}
        self._session = None

    def get_session_path(self, session):
        return self._path

    @asynccontextmanager
    async def ainitialize_session(self, session):
        yield


def test_edit_2fa_uses_stored_password_and_persists(tmp_path, monkeypatch):
    SetPasswordFunc = _load_func()
    from modules import tdata_import

    written = []
    monkeypatch.setattr(tdata_import, "write_2fa_password", lambda phone, pw: written.append((phone, pw)))

    path = str(tmp_path / "acc.jsession")
    # a full jsession on disk, so the rewrite round-trips through AccountSettings.asdict
    on_disk = {
        "auth_key": "a",
        "account": {"first_name": "Denis", "last_name": "A", "user_id": 1, "added_at": 0, "phone_number": "1"},
        "application": {"api_id": 1, "api_hash": "h", "device_name": "d", "app_version": "v", "sdk": "s", "lang_pack": "tdesktop", "system_lang_code": "en"},
        "proxy": None,
        "password": "old_pw",
    }
    Path = tmp_path / "acc.jsession"
    Path.write_text(json.dumps(on_disk))

    from modules.types.account_settings import AccountSettings
    js = type("J", (), {"account": AccountSettings.from_dict(on_disk)})()

    storage = _Storage(path, js)

    func = SetPasswordFunc.__new__(SetPasswordFunc)
    func.storage = storage
    func.settings = None
    func.sessions = []

    session = _Session()
    reports = []

    async def report(text):
        reports.append(text)

    asyncio.run(func.edit_2fa(session, "new_pw", report))

    assert session.called_with == ("old_pw", "new_pw")
    assert any("Successfully" in r for r in reports)
    # new password persisted to disk
    assert json.loads(Path.read_text())["password"] == "new_pw"
    # and next to the account's tdata
    assert written == [("1", "new_pw")]
