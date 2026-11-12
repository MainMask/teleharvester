"""Workers from Telethon .session files (modules/session_import.py)."""
import asyncio
import io
import os
import zipfile

import pytest
from telethon.crypto import AuthKey
from telethon.sessions import SQLiteSession, StringSession

from modules import projects, session_import, tdata_import


def _sqlite(path, key=b"k" * 256):
    session = SQLiteSession(path)
    session.set_dc(2, "149.154.167.51", 443)
    session.auth_key = AuthKey(key)
    session.save()
    session.close()
    return path if path.endswith(".session") else path + ".session"


def test_the_key_is_read_from_a_copy_as_a_string_session(tmp_path):
    path = _sqlite(str(tmp_path / "acc.session"))
    before = open(path, "rb").read()
    string = StringSession(session_import.read_key(path))
    assert string.dc_id == 2 and string.server_address == "149.154.167.51" and string.auth_key.key == b"k" * 256
    assert open(path, "rb").read() == before  # the seller's file untouched


def test_a_file_without_a_key_is_refused(tmp_path):
    session = SQLiteSession(str(tmp_path / "empty"))
    session.save()
    session.close()
    with pytest.raises(ValueError, match="нет ключа"):
        session_import.read_key(str(tmp_path / "empty.session"))


def test_each_session_is_found_with_its_json_also_in_a_zip(tmp_path):
    root = tmp_path / "in"
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("pack/111.session", b"x")
        zipped.writestr("pack/111.json", '{"app_id": 4, "twoFA": "pw"}')
        zipped.writestr("pack/222.session", b"y")
        zipped.writestr("pack/imported/333.session", b"z")  # imported before: left out
    assert tdata_import.unpack(archive.getvalue(), str(root)) is None  # no tdata in it: .session files
    found = session_import.collect(str(root))
    assert [(os.path.basename(path), meta) for path, meta in found] == [
        ("111.session", {"app_id": 4, "twoFA": "pw"}), ("222.session", {})]


def test_an_account_that_is_a_worker_elsewhere_is_refused_before_connecting(tmp_path, monkeypatch):
    path = _sqlite(str(tmp_path / "acc.session"))
    monkeypatch.setattr(projects, "find_account", lambda user_id=None, phone=None: "/else/7.jsession")
    monkeypatch.setattr(projects, "taken", lambda path: "уже воркер проекта «Other»")

    class _NeverClient:
        def __init__(self, *a, **k):
            raise AssertionError("connected")

    monkeypatch.setattr(session_import, "TelegramClient", _NeverClient)
    with pytest.raises(ValueError, match="Other"):
        asyncio.run(session_import.convert(path, {"user_id": 7}, None, str(tmp_path / "sessions")))


class _Storage:
    def __init__(self, directory, home):
        self.directory, self.home = directory, home
        self.jsessions_paths, self.added, self.phones = {}, [], {"200"}

    def add_jsession(self, path):
        self.added.append(path)

    def is_phone_exists(self, phone):
        return phone in self.phones


def test_import_all_adds_the_new_ones_and_moves_every_imported_file_aside(tmp_path, monkeypatch):
    root = tmp_path / "in"
    root.mkdir()
    for name in ("1.session", "1.json", "2.session", "3.session", "4.session"):
        (root / name).write_text("{}")

    async def convert(path, meta, proxy, sessions_dir):
        name = os.path.basename(path)
        if name == "3.session":
            raise ValueError("этот аккаунт уже воркер проекта «Other»")
        if name == "4.session":  # its file is in the project, but no worker is read from it
            return "400", False
        return ("100", True) if name == "1.session" else ("200", False)

    monkeypatch.setattr(session_import, "convert", convert)
    storage = _Storage(str(tmp_path / "sessions"), str(tmp_path / "home"))
    added, failed = asyncio.run(session_import.import_all(str(root), storage))
    assert added == storage.added == [os.path.join(storage.directory, "100.jsession")]
    assert [name for name, _ in failed] == ["2.session", "3.session", "4.session"]
    assert "уже воркер этого" in failed[0][1]
    assert "бот его не читает" in failed[2][1]
    # imported (or found already the project's): aside, never connected again; refused: left to fix
    assert sorted(os.listdir(root / "imported")) == ["1.json", "1.session", "2.session"]
    assert sorted(os.listdir(root)) == ["3.session", "4.session", "imported"]


def test_the_bot_imports_the_files_sent_and_waits_for_the_next_ones(tmp_path, monkeypatch):
    from types import SimpleNamespace as ns

    from bot.routers.accounts import upload
    from bot.states import ImportFiles

    class _State:
        def __init__(self):
            self.state, self.data = ImportFiles.files, {}

        async def set_state(self, state):
            self.state = state

        async def get_data(self):
            return dict(self.data)

        async def update_data(self, **kw):
            self.data.update(kw)

    class _Message:
        def __init__(self, name):
            self.answers = []
            self.document = ns(file_id="f", file_name=name, file_size=1)

            async def get_file(file_id, request_timeout=None):
                return ns(file_path="documents/file")

            async def download_file(file_path, timeout=30):
                return io.BytesIO(b"x")

            self.bot = ns(get_file=get_file, download_file=download_file)

        async def answer(self, text, **kwargs):
            self.answers.append(text)

    imported = []

    async def import_all(root, storage):
        imported.append(sorted(os.listdir(root)))
        return [], [("a.session", "сессия не авторизована")]

    monkeypatch.setattr(session_import, "import_all", import_all)
    manager = ns(project_id=1, active=False, acquire=lambda *a, **k: True, release=lambda: None)
    state, message = _State(), _Message("a.session")
    album = [message, _Message("a.json"), _Message("notes.pdf")]
    asyncio.run(upload.files_sent(message, state, ns(storage=None, count=lambda: 0), manager, album=album))
    assert imported == [["a.json", "a.session"]] and "notes.pdf" in message.answers[0]  # its .json beside it
    assert any("не авторизована" in text for text in message.answers)
    # ready for the next files at once
    assert state.state == ImportFiles.files and "следующие файлы" in message.answers[-1]


def test_after_a_session_import_every_account_goes_to_its_country_s_proxy(monkeypatch):
    from types import SimpleNamespace as ns

    from bot.routers.accounts import upload
    from modules import proxy_geo

    async def import_all(root, storage):
        return ["sessions/79161234567.jsession"], []

    rebalanced = []

    async def rebalance(storage, in_use):
        rebalanced.append((storage, in_use()))
        return "🌍 Прокси по странам: пересажено аккаунтов — 2."

    monkeypatch.setattr(session_import, "import_all", import_all)
    monkeypatch.setattr(proxy_geo, "rebalance", rebalance)
    monkeypatch.setattr(projects, "account_label", lambda path: path)
    storage = ns(jsessions_paths={"sessions/a.jsession": None, "sessions/b.jsession": None})
    pool = ns(storage=storage, count=lambda: 2, busy=lambda path: path == "sessions/a.jsession",
              scraped=lambda path: False)
    answers = []
    message = ns(answer=lambda text, **kw: asyncio.sleep(0, answers.append(text)))
    manager = ns(acquire=lambda *a, **k: True, release=lambda: None)
    asyncio.run(upload._import_sessions(message, pool, manager, "root"))
    assert rebalanced == [(storage, {"sessions/a.jsession"})]  # the one in use left as it is
    assert answers[-1] == "🌍 Прокси по странам: пересажено аккаунтов — 2."
