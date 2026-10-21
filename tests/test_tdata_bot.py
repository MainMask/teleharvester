"""Offline tests for the bot tdata-upload flow (bot/routers/accounts/tdata.py)."""

import asyncio
import conftest
import io
import types
import zipfile

import pytest

from bot.routers import accounts
from modules.types.proxy import Proxy


def ns(**kw):
    return types.SimpleNamespace(**kw)


def _zip_bytes(with_tdata=True):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        if with_tdata:
            z.writestr("tdata/key_datas", b"x")
            z.writestr("tdata/D877F783D5D3EF8Cs", b"x")
        else:
            z.writestr("notes.txt", b"nothing here")
    return buf.getvalue()


class _State:
    def __init__(self, data=None):
        self.data = data or {}
        self.state = None
        self.cleared = False

    async def set_state(self, state):
        self.state = state

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_data(self):
        return self.data

    async def clear(self):
        self.cleared = True


class _Msg:
    def __init__(self, text=None, document=None, zip_bytes=b""):
        self.text = text
        self.document = document
        self._zip = zip_bytes
        self.replies = []
        self.deleted = False
        self.bot = ns(download=self._download)

    async def delete(self):
        self.deleted = True

    async def _download(self, document):
        return io.BytesIO(self._zip)

    async def answer(self, text, **kwargs):
        self.replies.append(text)


class _Storage:
    directory = "sessions"  # the project's (projects.sessions_dir)
    home = property(lambda self: conftest.home())

    def __init__(self, phones=()):
        self.jsessions_paths = {}
        self.added = None
        self.phones = set(phones)

    def is_phone_exists(self, phone):
        return phone in self.phones

    def add_jsession(self, path):
        self.added = path
        # a new session, as loaded back from its file
        return ns(account=ns(proxy=Proxy("socks5", "10.0.0.7", 1080), password="from-file"))


class _Pool:
    def __init__(self, storage):
        self.storage = storage

    def count(self):
        return 1


class _Manager:
    project_id = 1

    def __init__(self, free=True):
        self._free = free
        self.label = "Рассылка"
        self.released = False

    def acquire(self, label, cancelable=True, timeout=600):
        return self._free

    def release(self):
        self.released = True


def test_archive_step_rejects_non_zip():
    msg = _Msg(document=ns(file_size=10), zip_bytes=b"not a zip")
    state = _State()
    asyncio.run(accounts.tdata.tdata_archive(msg, state))
    assert any("не ZIP" in r or "ZIP" in r for r in msg.replies)
    assert state.state is None  # did not advance to password


def test_archive_step_accepts_zip_and_asks_password():
    data = _zip_bytes()
    msg = _Msg(document=ns(file_size=len(data)), zip_bytes=data)
    state = _State()
    asyncio.run(accounts.tdata.tdata_archive(msg, state))
    assert state.data.get("zip") == data
    assert any("2FA" in r for r in msg.replies)


def test_password_step_happy_path(monkeypatch):
    storage = _Storage()
    msg = _Msg(text="secret")
    state = _State({"zip": _zip_bytes()})
    manager = _Manager(free=True)

    captured = {}

    async def fake_convert(tdata_dir, proxy, password, sessions_dir):
        captured["password"] = password
        captured["tdata_dir"] = tdata_dir
        return "79990001122"

    monkeypatch.setattr(accounts.tdata.tdata_import, "convert", fake_convert)
    monkeypatch.setattr(accounts.tdata.tdata_import, "load_proxies", lambda path: [])

    asyncio.run(accounts.tdata.tdata_password(msg, state, _Pool(storage), manager))

    assert captured["password"] == "secret" and msg.deleted  # not left in the chat
    assert storage.added == "sessions/79990001122.jsession"
    assert manager.released is True
    assert state.cleared is True
    assert any("импортирован" in r.lower() for r in msg.replies)
    # the summary reports what the loaded file holds
    assert any("socks5://10.0.0.7:1080" in r and "2FA: да" in r for r in msg.replies)


def test_password_step_account_already_loaded(monkeypatch):
    storage = _Storage(phones=["79990001122"])  # loaded from a file not named after its phone
    msg = _Msg(text="-")
    manager = _Manager(free=True)

    async def fake_convert(tdata_dir, proxy, password, sessions_dir):
        return "79990001122"

    monkeypatch.setattr(accounts.tdata.tdata_import, "convert", fake_convert)
    monkeypatch.setattr(accounts.tdata.tdata_import, "load_proxies", lambda path: [])

    asyncio.run(accounts.tdata.tdata_password(msg, _State({"zip": _zip_bytes()}), _Pool(storage), manager))

    assert storage.added is None
    assert any("уже есть" in r for r in msg.replies) and manager.released is True


def test_password_step_refuses_when_busy(monkeypatch):
    storage = _Storage()
    msg = _Msg(text="-")
    state = _State({"zip": _zip_bytes()})
    manager = _Manager(free=False)

    asyncio.run(accounts.tdata.tdata_password(msg, state, _Pool(storage), manager))

    assert storage.added is None
    assert any("Занят" in r for r in msg.replies)


def test_password_step_no_tdata_in_zip(monkeypatch):
    storage = _Storage()
    msg = _Msg(text="-")
    state = _State({"zip": _zip_bytes(with_tdata=False)})
    manager = _Manager(free=True)

    asyncio.run(accounts.tdata.tdata_password(msg, state, _Pool(storage), manager))

    assert storage.added is None
    assert any("не найдена папка tdata" in r for r in msg.replies)
    assert manager.released is True


def test_password_step_not_authorized(monkeypatch):
    storage = _Storage()
    msg = _Msg(text="-")
    state = _State({"zip": _zip_bytes()})
    manager = _Manager(free=True)

    async def fake_convert(*a, **k):
        return None

    monkeypatch.setattr(accounts.tdata.tdata_import, "convert", fake_convert)
    monkeypatch.setattr(accounts.tdata.tdata_import, "load_proxies", lambda path: [])

    asyncio.run(accounts.tdata.tdata_password(msg, state, _Pool(storage), manager))

    assert storage.added is None
    assert any("не авторизован" in r for r in msg.replies)


def test_password_step_rejects_non_text(monkeypatch):
    storage = _Storage()
    msg = _Msg(text=None)  # a sticker/photo: no text
    state = _State({"zip": _zip_bytes()})
    manager = _Manager(free=True)

    async def fake_convert(*a, **k):
        raise AssertionError("must not import on a non-text reply")

    monkeypatch.setattr(accounts.tdata.tdata_import, "convert", fake_convert)

    asyncio.run(accounts.tdata.tdata_password(msg, state, _Pool(storage), manager))

    assert storage.added is None
    assert state.cleared is False  # still waiting for the password
    assert any("Нужен пароль текстом" in r for r in msg.replies)


def test_password_step_frees_the_slot_when_no_temp_dir(monkeypatch):
    # the slot is non-cancelable: a full disk at mkdtemp must not hold it until a restart
    def full_disk(prefix):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(accounts.tdata.tempfile, "mkdtemp", full_disk)
    msg = _Msg(text="-")
    manager = _Manager(free=True)

    asyncio.run(accounts.tdata.tdata_password(msg, _State({"zip": _zip_bytes()}), _Pool(_Storage()), manager))

    assert manager.released is True
    assert any("Ошибка импорта" in r for r in msg.replies)


def test_password_step_refuses_an_archive_that_unpacks_too_big(monkeypatch):
    storage = _Storage()
    msg = _Msg(text="-")
    manager = _Manager(free=True)
    monkeypatch.setattr(accounts.tdata.tdata_import, "MAX_UNZIPPED", 1)  # the test archive: two files, 2 bytes
    monkeypatch.setattr(accounts.tdata.tdata_import.zipfile.ZipFile, "extractall",
                        lambda *a, **k: pytest.fail("unpacked"))

    asyncio.run(accounts.tdata.tdata_password(msg, _State({"zip": _zip_bytes()}), _Pool(storage), manager))

    assert storage.added is None and manager.released is True
    assert any("слишком велик" in r for r in msg.replies)


def test_password_with_spaces_is_kept_as_typed(monkeypatch):
    # as at the sign-in by phone: spaces may be part of a 2FA password, the stored one must match
    msg = _Msg(text=" secret ")
    captured = {}

    async def fake_convert(tdata_dir, proxy, password, sessions_dir):
        captured["password"] = password
        return "79990001122"

    monkeypatch.setattr(accounts.tdata.tdata_import, "convert", fake_convert)
    monkeypatch.setattr(accounts.tdata.tdata_import, "load_proxies", lambda path: [])

    asyncio.run(accounts.tdata.tdata_password(msg, _State({"zip": _zip_bytes()}), _Pool(_Storage()),
                                        _Manager(free=True)))

    assert captured["password"] == " secret "
