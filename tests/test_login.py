"""Login codes and adding an account by phone (bot/services/login.py, bot/routers/login.py)."""

import asyncio
import json
import types
from datetime import datetime, timedelta, timezone

import pytest
from telethon import errors
from telethon.tl.types.auth import SentCodeTypeApp

from bot.routers import login as router
from bot.services import login as sign_in
from bot.states import AddByPhone
from modules.types.proxy import Proxy

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def ns(**kw):
    return types.SimpleNamespace(**kw)


@pytest.fixture(autouse=True)
def _no_logins(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sign_in, "_logins", {})


# --- login codes -----------------------------------------------------------------

def test_code_extraction_and_spacing():
    assert sign_in.extract_code("Login code: 12345. Do not give this code to anyone") == "12345"
    assert sign_in.extract_code("Код для входа в Telegram: 654321") == "654321"
    assert sign_in.extract_code("no code 1234 or 1234567") is None
    assert sign_in.spaced("12345") == "1 2 3 4 5"


def test_only_codes_of_the_last_minutes():
    messages = [ns(message="Login code: 11111", date=NOW - timedelta(minutes=2)),
                ns(message="Login code: 22222", date=NOW - timedelta(minutes=40)),
                ns(message="New login from Linux", date=NOW)]
    assert sign_in.recent_codes(messages, NOW) == [("11111", NOW - timedelta(minutes=2))]


class _Worker:
    def __init__(self, messages=()):
        self.messages, self.connected = list(messages), False
        self.session = ns(_entities={1})

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def iter_dialogs(self, limit):
        yield ns(entity=ns(id=42))
        yield ns(entity=ns(id=sign_in.SERVICE_ID))

    async def get_messages(self, entity, limit):
        assert entity.id == sign_in.SERVICE_ID
        return self.messages


def _pool(busy=False, scraping=None):
    return ns(busy=lambda path: busy, scraping=scraping)


def test_free_worker_read_then_disconnected_and_cache_cleared():
    worker = _Worker([ns(message="Login code: 12345", date=datetime.now(timezone.utc))])
    codes = asyncio.run(sign_in.fetch_codes(_pool(), worker, "sessions/w.jsession"))
    assert [code for code, _ in codes] == ["12345"]
    assert not worker.connected and worker.session._entities == set()


def test_busy_worker_stays_connected():
    worker = _Worker()
    asyncio.run(sign_in.fetch_codes(_pool(busy=True), worker, "sessions/w.jsession"))
    assert worker.connected  # its job (or the auto-reply) disconnects it


def test_scraping_worker_is_not_connected():
    worker = _Worker()
    account = ns(path="sessions/w.jsession", label="W", client=worker)
    text, _ = asyncio.run(router._codes(_pool(scraping=ns(path=account.path)), account, 0))
    assert "занят скрапом" in text and not worker.connected


def test_codes_text_spaces_digits():
    text = router._codes_text(ns(label="Ann (@ann)"), [("12345", NOW - timedelta(minutes=3))], NOW)
    assert "<code>1 2 3 4 5</code> · 3 мин назад" in text and "12345" not in text


# --- adding by phone ---------------------------------------------------------------

def test_phone_and_code_parsing():
    assert sign_in.normalize_phone("+7 (999) 123-45-67") == "79991234567"
    assert sign_in.normalize_phone("12345") is None
    assert sign_in.code_digits("1 2-3 4 5") == "12345"
    assert sign_in.code_digits("abc") is None


class _Client:
    def __init__(self, sign_in_errors=(), me=None):
        self.errors = list(sign_in_errors)  # raised by the sign_in calls, in turn
        self.me = me or ns(id=555, phone="79991234567", first_name="Ann", last_name=None, username="ann")
        self.connected = self.logged_out = False
        self.codes_sent = 0
        self.signed = []
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
        self.signed.append((code, password))
        if self.errors:
            raise self.errors.pop(0)
        return self.me

    async def log_out(self):
        self.logged_out = True


class _Storage:
    def __init__(self, phones=(), proxies=()):
        self.phones = set(phones)
        self.jsessions_paths = {f"p{i}": ns(account=ns(proxy=p)) for i, p in enumerate(proxies)}
        self.added = []
        self.json_sessions = []

    def is_phone_exists(self, phone):
        return phone in self.phones

    def add_jsession(self, path):
        self.added.append(path)
        return object()


class _Msg:
    def __init__(self, text=None, chat_id=7):
        self.text, self.chat = text, ns(id=chat_id)
        self.answers, self.deleted = [], False

    async def answer(self, text, **kw):
        self.answers.append(text)

    async def delete(self):
        self.deleted = True


class _State:
    def __init__(self):
        self.state, self.cleared = None, False

    async def set_state(self, state):
        self.state = state

    async def clear(self):
        self.state, self.cleared = None, True


def _start(monkeypatch, client, storage=None, personal=None, proxy=None):
    """phone_input with `client` as the new sign-in's client; (state, pool, msg)."""
    storage = storage or _Storage()
    pool = ns(storage=storage, count=lambda: len(storage.added))
    monkeypatch.setattr(sign_in, "new_client", lambda p: (client, ns(lang_pack="android")))
    monkeypatch.setattr(router.tdata_import, "load_proxies", lambda path: [proxy] if proxy else [])
    state, msg = _State(), _Msg("+7 999 123 45 67")

    async def go():
        await router.phone_input(msg, state, pool, personal)
    asyncio.run(go())
    return state, pool, msg


def _send(handler, text, state, pool, personal=None):
    msg = _Msg(text)

    async def go():
        await handler(msg, state, pool, personal)
    asyncio.run(go())
    return msg


def test_signs_in_saves_and_joins_the_pool(monkeypatch, tmp_path):
    proxy = Proxy("socks5", "1.1.1.1", 1080, "u", "p")
    client = _Client()
    state, pool, msg = _start(monkeypatch, client, proxy=proxy)
    assert state.state == AddByPhone.code and client.codes_sent == 1 and "1 2 3 4 5" in msg.answers[-1]

    reply = _send(router.code_input, "1 2-3 4 5", state, pool)

    assert client.signed == [("12345", None)] and reply.deleted
    saved = json.loads((tmp_path / "sessions" / "79991234567.jsession").read_text())
    assert saved["auth_key"] == "KEY" and saved["proxy"]["ip"] == "1.1.1.1" and saved["password"] is None
    assert saved["application"]["lang_pack"] == "android" and saved["account"]["user_id"] == 555
    assert pool.storage.added == ["sessions/79991234567.jsession"]
    assert sign_in._logins == {} and not client.connected and state.cleared
    assert "Аккаунт добавлен" in reply.answers[-1]


def test_two_step_password(monkeypatch, tmp_path):
    client = _Client([errors.SessionPasswordNeededError(request=None),
                      errors.PasswordHashInvalidError(request=None)])
    state, pool, _ = _start(monkeypatch, client)

    _send(router.code_input, "12345", state, pool)
    assert state.state == AddByPhone.password
    wrong = _send(router.password_input, "bad", state, pool)
    assert "Неверный пароль" in wrong.answers[-1] and wrong.deleted
    _send(router.password_input, " my pass ", state, pool)

    saved = json.loads((tmp_path / "sessions" / "79991234567.jsession").read_text())
    assert saved["password"] == " my pass " and client.signed[-1] == (None, " my pass ")


def test_wrong_code_keeps_the_sign_in(monkeypatch):
    client = _Client([errors.PhoneCodeInvalidError(request=None)])
    state, pool, _ = _start(monkeypatch, client)
    reply = _send(router.code_input, "11111", state, pool)
    assert "Неверный код" in reply.answers[-1] and 7 in sign_in._logins and client.connected


def test_expired_code_offers_a_new_one(monkeypatch):
    client = _Client([errors.PhoneCodeExpiredError(request=None)])
    state, pool, _ = _start(monkeypatch, client)
    reply = _send(router.code_input, "11111", state, pool)
    assert "истёк" in reply.answers[-1] and 7 in sign_in._logins

    callback = ns(message=_Msg(), answer=lambda *a, **k: asyncio.sleep(0))
    asyncio.run(router.phone_resend(callback, state))
    assert client.codes_sent == 2 and state.state == AddByPhone.code


@pytest.mark.parametrize("where", ["workers", "personal"])
def test_known_number_gets_no_code(monkeypatch, where):
    client = _Client()
    storage = _Storage(phones={"79991234567"}) if where == "workers" else _Storage()
    personal = _Storage(phones={"+79991234567"}) if where == "personal" else None
    state, _, msg = _start(monkeypatch, client, storage=storage, personal=personal)
    assert client.codes_sent == 0 and not client.connected and state.cleared
    assert sign_in._logins == {}


def test_full_proxies_get_no_code(monkeypatch):
    proxy = Proxy("socks5", "1.1.1.1", 1080, None, None)
    client = _Client()
    state, _, msg = _start(monkeypatch, client, storage=_Storage(proxies=[proxy] * 3), proxy=proxy)
    assert client.codes_sent == 0 and "прокси заняты" in msg.answers[-1]


def test_personal_account_found_after_sign_in_is_logged_out(monkeypatch, tmp_path):
    client = _Client()
    personal = _Storage()
    personal.json_sessions = [ns(account=ns(account=ns(user_id=555)))]
    state, pool, _ = _start(monkeypatch, client, personal=personal)
    _send(router.code_input, "12345", state, pool, personal)
    assert client.logged_out and pool.storage.added == [] and not (tmp_path / "sessions").exists()


def test_cancel_closes_the_sign_in_at_once(monkeypatch):
    from bot.routers import menu

    client = _Client()
    state, pool, _ = _start(monkeypatch, client)
    assert client.connected and 7 in sign_in._logins

    class _FormState(_State):
        async def get_state(self):
            return AddByPhone.code

    msg = _Msg("/cancel")
    asyncio.run(menu.cancel(msg, _FormState(), ns(active=False)))
    assert not client.connected and sign_in._logins == {} and msg.answers[-1] == "Отменено."


def test_abandoned_sign_in_is_closed(monkeypatch):
    monkeypatch.setattr(sign_in, "LOGIN_TIMEOUT", 0)
    client = _Client()

    async def go():
        sign_in.open_login(7, sign_in.Login(client, "79991234567", None, ns(lang_pack="")))
        client.connected = True
        await asyncio.sleep(0.01)
    asyncio.run(go())
    assert sign_in._logins == {} and not client.connected
