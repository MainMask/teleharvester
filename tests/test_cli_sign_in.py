"""The terminal menu's sign-in (functions/sign_in.py) and personal accounts (functions/accounts.py):
the bot's rules (modules/login.py)."""

import asyncio
import contextlib
import json
import types
from datetime import datetime, timezone

import pytest
from telethon import errors
from telethon.tl.types.auth import SentCodeTypeApp

from functions import accounts as accounts_cli
from functions import sign_in as cli
from modules import json_file, login as sign_in
from modules.types.proxy import Proxy


def ns(**kw):
    return types.SimpleNamespace(**kw)


SETTINGS = ns(api_id=1, api_hash="h")


@pytest.fixture(autouse=True)
def _in_tmp(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)


class _Console:
    """Typed answers, in turn; what was printed."""

    def __init__(self, answers):
        self.answers, self.printed = list(answers), []

    def input(self, prompt=""):
        return self.answers.pop(0)

    def print(self, text="", *a, **k):
        self.printed.append(str(text))

    @property
    def text(self):
        return "\n".join(self.printed)


class _Client:
    def __init__(self, sign_in_errors=(), me=None):
        self.errors = list(sign_in_errors)
        self.me = me or ns(id=555, phone="79991234567", first_name="Ann", last_name=None, username="ann")
        self.connected = self.logged_out = False
        self.codes_sent = 0
        self.api_id, self.api_hash = 6, "hash"
        self._init_request = ns(device_model="Pixel", app_version="8.8.5", system_version="10 Q (29)",
                                system_lang_code="en", lang_pack="")
        self.session = ns(save=lambda: "KEY")

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def send_code_request(self, phone):
        self.codes_sent += 1
        return ns(type=SentCodeTypeApp(length=5))

    async def sign_in(self, phone=None, code=None, password=None):
        if self.errors:
            raise self.errors.pop(0)
        return self.me

    async def log_out(self):
        self.logged_out, self.connected = True, False
        return True


class _Workers:
    def __init__(self, phones=()):
        self.phones, self.added, self.initialize = set(phones), [], False
        self.jsessions_paths, self.json_sessions = {}, []

    def is_phone_exists(self, phone):
        return phone in self.phones

    def add_jsession(self, path):
        self.added.append(path)
        return object()

    def __len__(self):
        return len(self.added)

    sessions = []


def _add(monkeypatch, answers, client, workers=None, proxy=None, passwords=(), personal=None):
    """AddByPhoneFunc with typed `answers` and `client` as the fresh sign-in client."""
    console, passwords = _Console(answers), list(passwords)
    personal = personal if personal is not None else _Workers()
    monkeypatch.setattr(cli, "personal_storage", lambda api_id, api_hash: personal)
    monkeypatch.setattr(cli, "console", console)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": passwords.pop(0))
    monkeypatch.setattr(sign_in, "new_client", lambda p: (client, ns(lang_pack="android")))
    monkeypatch.setattr(sign_in.tdata_import, "load_proxies", lambda path: [proxy] if proxy else [])
    workers = workers if workers is not None else _Workers()  # an empty one is falsy (__len__)
    asyncio.run(cli.AddByPhoneFunc(workers, SETTINGS).execute())
    return console, workers


def test_a_worker_gets_a_pool_proxy_and_keeps_its_password(monkeypatch, tmp_path):
    client = _Client([errors.SessionPasswordNeededError(request=None)])
    proxy = Proxy("socks5", "1.1.1.1", 1080, "u", "p")
    console, workers = _add(monkeypatch, ["1", "+7 999 123 45 67", "12345"], client, proxy=proxy,
                            passwords=["secret"])

    saved = json.loads((tmp_path / "sessions" / "79991234567.jsession").read_text())
    assert saved["proxy"]["ip"] == "1.1.1.1" and saved["password"] == "secret"
    assert workers.added == ["sessions/79991234567.jsession"] and "✅ Аккаунт добавлен" in console.text
    assert not client.connected


def test_a_personal_account_has_no_proxy_nor_password(monkeypatch, tmp_path):
    client, personal = _Client([errors.SessionPasswordNeededError(request=None)]), _Workers()
    proxy = Proxy("socks5", "1.1.1.1", 1080, "u", "p")
    console, workers = _add(monkeypatch, ["2", "+79991234567", "1 2 3 4 5"], client, proxy=proxy,
                            passwords=["secret"], personal=personal)

    saved = json.loads((tmp_path / "personal_sessions" / "79991234567.jsession").read_text())
    assert saved["proxy"] is None and saved["password"] is None
    assert personal.added == [str(tmp_path / "personal_sessions" / "79991234567.jsession")]
    assert workers.added == [] and "пароль не сохранён" in console.text


def test_a_known_number_gets_no_code(monkeypatch):
    client = _Client()
    console, _ = _add(monkeypatch, ["2", "+79991234567"], client, workers=_Workers(phones={"79991234567"}))
    assert client.codes_sent == 0 and "личным аккаунтом он не станет" in console.text


def test_known_after_sign_in_is_logged_out(monkeypatch, tmp_path):
    client = _Client(me=ns(id=1, phone="70000000000", first_name="A", last_name=None, username=None))
    console, workers = _add(monkeypatch, ["1", "+79991234567", "12345"], client,
                            workers=_Workers(phones={"70000000000"}))
    assert client.logged_out and workers.added == [] and "вход отменён" in console.text
    assert not (tmp_path / "sessions").exists()


def test_empty_input_resends_and_q_cancels(monkeypatch):
    client = _Client()
    console, workers = _add(monkeypatch, ["1", "+79991234567", "", "q"], client)
    assert client.codes_sent == 2 and "Новый код отправлен" in console.text
    assert workers.added == [] and not client.connected


# --- login codes ----------------------------------------------------------------------------

class _Account:
    def __init__(self, messages):
        self.messages, self.connected = messages, False

    async def iter_dialogs(self, limit):
        yield ns(entity=ns(id=sign_in.SERVICE_ID))

    async def get_messages(self, entity, limit):
        return self.messages


class _Storage:
    """One worker, connected only inside ainitialize_session (initialize=False)."""

    def __init__(self, path, client):
        self.path, self.client, self.jsessions_paths = path, client, {}

    @property
    def sessions(self):
        return [self.client]

    def get_session_path(self, client):
        return self.path

    @contextlib.asynccontextmanager
    async def ainitialize_session(self, client):
        client.connected = True
        try:
            yield
        finally:
            client.connected = False


def test_codes_of_a_given_file(monkeypatch):
    account = _Account([ns(message="Login code: 12345", date=datetime.now(timezone.utc))])
    console = _Console(["n"])
    monkeypatch.setattr(cli, "console", console)
    asyncio.run(cli.LoginCodesFunc(_Storage("sessions/w.jsession", account), SETTINGS)
                .execute("sessions/./w.jsession"))
    assert "1 2 3 4 5 · только что" in console.text and not account.connected


def test_codes_of_an_unknown_file(monkeypatch):
    console = _Console([])
    monkeypatch.setattr(cli, "console", console)
    asyncio.run(cli.LoginCodesFunc(_Storage("sessions/w.jsession", _Account([])), SETTINGS)
                .execute("sessions/other.jsession"))
    assert "не найден" in console.text


# --- personal accounts: listed, removed -------------------------------------------------------

class _Personal:
    def __init__(self, path, client):
        self.full_sessions, self.jsessions_paths = {path: client}, {}

    @property
    def sessions(self):
        return list(self.full_sessions.values())

    def get_session_path(self, client):
        return next(p for p, c in self.full_sessions.items() if c is client)

    def forget_session(self, path):
        self.full_sessions.pop(path, None)


def _personal(monkeypatch, tmp_path, answers):
    (tmp_path / "personal_sessions").mkdir()
    path = "personal_sessions/me.jsession"
    (tmp_path / path).write_text("{}")
    me, console = _Client(), _Console(answers)
    personal = _Personal(path, me)
    monkeypatch.setattr(accounts_cli, "personal_storage", lambda api_id, api_hash: personal)
    monkeypatch.setattr(accounts_cli, "console", console)
    return personal, me, path, console


def test_the_accounts_list_shows_personal_ones_without_connecting(monkeypatch, tmp_path):
    personal, me, _, console = _personal(monkeypatch, tmp_path, [])
    console.status = lambda *a: contextlib.nullcontext()
    asyncio.run(accounts_cli.AccountsFunc(_Workers(), SETTINGS).execute())
    assert "Личные (только для скрапа): 1" in console.text and "me.jsession" in console.text
    assert not me.connected


def test_removal_logs_out_and_deletes(monkeypatch, tmp_path):
    personal, me, path, console = _personal(monkeypatch, tmp_path, ["1", "y"])
    asyncio.run(accounts_cli.RemovePersonalFunc(_Workers(), SETTINGS).execute())
    assert me.logged_out and not (tmp_path / path).exists() and personal.sessions == []
    assert "✅" in console.printed[-1]


def test_no_removal_with_an_unfinished_bot_scrape(monkeypatch, tmp_path):
    from modules import scraped_files

    personal, me, path, console = _personal(monkeypatch, tmp_path, ["1", "y"])
    json_file.save(scraped_files.SCRAPE_MARKER, {"account": path})
    asyncio.run(accounts_cli.RemovePersonalFunc(_Workers(), SETTINGS).execute())
    assert "незавершённый скрап" in console.printed[-1] and not me.logged_out and (tmp_path / path).exists()


def test_no_removal_with_a_checkpoint_left_by_a_stop(monkeypatch, tmp_path):
    personal, me, path, console = _personal(monkeypatch, tmp_path, ["1", "y"])
    resume = tmp_path / "assets" / "databases" / "news_partial" / "checkpoint" / "resume.json"
    resume.parent.mkdir(parents=True)
    resume.write_text(json.dumps({"name": "news", "account": path}))
    asyncio.run(accounts_cli.RemovePersonalFunc(_Workers(), SETTINGS).execute())
    assert "незавершённый скрап" in console.printed[-1] and not me.logged_out and (tmp_path / path).exists()


def test_codes_give_up_on_a_connect_that_hangs(monkeypatch):
    """A dead proxy: connect() inside the timeout too, as the bot's fetch_codes."""
    class _Hanging(_Storage):
        @contextlib.asynccontextmanager
        async def ainitialize_session(self, client):
            await asyncio.sleep(3600)
            yield

    console = _Console([])
    monkeypatch.setattr(cli, "console", console)
    monkeypatch.setattr(sign_in, "FETCH_TIMEOUT", 0.05)
    asyncio.run(asyncio.wait_for(cli.LoginCodesFunc(_Hanging("sessions/w.jsession", _Account([])), SETTINGS)
                                 .execute("sessions/w.jsession"), 2))
    assert "не удалось подключиться (TimeoutError)" in console.text


def test_removal_of_an_account_whose_file_is_already_gone(monkeypatch, tmp_path):
    personal, me, path, console = _personal(monkeypatch, tmp_path, [])
    (tmp_path / path).unlink()  # removed by hand meanwhile
    account = accounts_cli.scrape_accounts(None, personal)[0]
    assert asyncio.run(sign_in.remove_personal(personal, account)) and personal.sessions == []
