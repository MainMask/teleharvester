"""Workers go by username (Curator1, Curator2, … Curator10) in the pool and in reports."""

import asyncio
import contextlib
import json
import types

from telethon.crypto import AuthKey
from telethon.sessions import StringSession

from modules.storages.sessions_storage import SessionsStorage
from modules.types.account_settings import AccountSettings


def ns(**kw):
    return types.SimpleNamespace(**kw)


def _auth_key():
    session = StringSession()
    session.set_dc(2, "149.154.167.51", 443)
    session.auth_key = AuthKey(bytes(256))
    return session.save()


def _write_jsession(directory, phone, username):
    AccountSettings.from_dict({
        "auth_key": _auth_key(),
        "account": {"first_name": "W", "last_name": "", "user_id": int(phone),
                    "added_at": 0.0, "phone_number": phone, "username": username},
        "application": {"api_id": 6, "api_hash": "h", "device_name": "PC", "app_version": "1",
                        "sdk": "Linux", "lang_pack": "tdesktop", "system_lang_code": "en"},
        "proxy": None,
    }).save(str(directory / f"{phone}.jsession"))


def _storage(tmp_path, accounts):
    for phone, username in accounts:
        _write_jsession(tmp_path, phone, username)
    return SessionsStorage(str(tmp_path), 1, "x", initialize=False)


def _usernames(storage):
    by_client = {client: path for path, client in storage.full_sessions.items()}
    return [storage.usernames.get(by_client[client]) for client in storage.sessions]


def test_pool_is_ordered_by_username_not_by_phone(tmp_path):
    storage = _storage(tmp_path, [("1", "Curator3"), ("2", None), ("3", "Curator10"),
                                  ("4", "Curator1"), ("5", "Curator2")])

    assert _usernames(storage) == ["Curator1", "Curator2", "Curator3", "Curator10", None]


def test_remember_username_reorders_and_persists(tmp_path):
    storage = _storage(tmp_path, [("1", "Curator2"), ("2", None)])
    unnamed = storage.full_sessions[str(tmp_path / "2.jsession")]

    storage.remember_username(unnamed, "Curator1")

    assert _usernames(storage) == ["Curator1", "Curator2"]
    saved = json.loads((tmp_path / "2.jsession").read_text())
    assert saved["account"]["username"] == "Curator1"
    assert SessionsStorage(str(tmp_path), 1, "x", initialize=False).usernames[
        str(tmp_path / "2.jsession")] == "Curator1"


def test_remember_same_username_does_not_rewrite_the_file(tmp_path, monkeypatch):
    storage = _storage(tmp_path, [("1", "Curator1")])
    client = storage.sessions[0]
    monkeypatch.setattr(AccountSettings, "save", lambda *a: (_ for _ in ()).throw(AssertionError))

    storage.remember_username(client, "Curator1")


def test_remember_name_persists(tmp_path):
    storage = _storage(tmp_path, [("1", "Curator1")])
    path = str(tmp_path / "1.jsession")

    storage.remember_name(storage.full_sessions[path], "Ivan", "Petrov")

    assert storage.jsessions_paths[path].account.account.first_name == "Ivan"
    saved = json.loads((tmp_path / "1.jsession").read_text())
    assert (saved["account"]["first_name"], saved["account"]["last_name"]) == ("Ivan", "Petrov")


def test_remember_same_name_does_not_rewrite_the_file(tmp_path, monkeypatch):
    storage = _storage(tmp_path, [("1", "Curator1")])
    monkeypatch.setattr(AccountSettings, "save", lambda *a: (_ for _ in ()).throw(AssertionError))

    storage.remember_name(storage.sessions[0], "W", "")


def test_fetch_me_refreshes_a_stale_stored_name(tmp_path):
    storage = _storage(tmp_path, [("1", "Curator1")])
    client = storage.sessions[0]

    @contextlib.asynccontextmanager
    async def ainitialize_session(session):
        yield

    async def get_me():
        return ns(username="Curator1", first_name="Ivan", last_name=None)

    storage.ainitialize_session = ainitialize_session
    client.get_me = get_me
    asyncio.run(storage.fetch_me(client))

    saved = json.loads((tmp_path / "1.jsession").read_text())
    assert (saved["account"]["first_name"], saved["account"]["last_name"]) == ("Ivan", None)


def test_changename_remembers_the_new_name():
    from functions.changename import ChangeNameFunc

    class _Session:
        async def get_me(self):
            return ns(first_name="Old", last_name=None)

        async def __call__(self, request):
            return None

    @contextlib.asynccontextmanager
    async def ainitialize_session(session):
        yield

    remembered = []
    storage = ns(sessions=[], ainitialize_session=ainitialize_session,
                 remember_name=lambda session, first, last: remembered.append((first, last)))
    fn = ChangeNameFunc(storage, ns(delay=[0]))

    async def report(text):
        pass

    asyncio.run(fn.change_name(_Session(), report, first_name="Ivan", last_name=None))

    assert remembered == [("Ivan", None)]


def _fn(sessions):
    from functions.base.base import BaseFunction

    fn = BaseFunction()
    fn.sessions = sessions
    return fn


def test_gather_in_order_holds_a_faster_worker_s_lines():
    msgs = []

    async def report(text):
        msgs.append(text)

    async def work(session, report):
        await report(f"{session} start")
        await asyncio.sleep(0.03 if session == "w1" else 0)  # w2 and w3 finish first
        await report(f"{session} done")
        return session.upper()

    results = asyncio.run(_fn(["w1", "w2", "w3"]).gather_in_order(work, report))

    assert msgs == ["w1 start", "w1 done", "w2 start", "w2 done", "w3 start", "w3 done"]
    assert results == ["W1", "W2", "W3"]


def test_gather_in_order_survives_a_failing_worker():
    msgs = []

    async def report(text):
        msgs.append(text)

    async def work(session, report):
        await asyncio.sleep(0.02 if session == "w1" else 0)
        await report(session)
        if session == "w1":
            raise RuntimeError("boom")

    try:
        asyncio.run(_fn(["w1", "w2"]).gather_in_order(work, report))
    except RuntimeError:
        pass

    assert msgs == ["w1", "w2"]


def test_status_check_reports_in_username_order(tmp_path, monkeypatch):
    from functions.spamblock import SpamBlockFunc

    monkeypatch.chdir(tmp_path)

    class _Worker:
        def __init__(self, n):
            self.n = n

        async def get_me(self):
            return ns(id=self.n, username=f"CitadelCurator{self.n}", first_name="C", last_name=None)

        def conversation(self, *_):
            n = self.n

            class _Conv:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *exc):
                    return False

                async def send_message(self, text):
                    pass

                async def get_response(self):
                    await asyncio.sleep(0.01 * (6 - n))  # the last worker answers first
                    return ns(message="Good news, no limits are currently applied.")

            return _Conv()

    workers = [_Worker(n) for n in range(1, 6)]
    storage = ns(
        sessions=workers,
        get_session_path=lambda s: None,
        remember_username=lambda s, u: None,
        remember_name=lambda s, first, last: names.append((first, last)),
    )
    names = []

    @contextlib.asynccontextmanager
    async def ainitialize_session(session):
        yield

    storage.ainitialize_session = ainitialize_session
    fn = SpamBlockFunc(storage, ns(delay=[0]))
    msgs = []

    async def report(text):
        msgs.append(text)

    asyncio.run(fn.scan(report))

    assert msgs == [f"[+] [@CitadelCurator{n}] Account active (no restriction)" for n in range(1, 6)]
    assert names == [("C", None)] * 5  # each worker's live name is kept for the account pickers


def test_gather_in_order_keeps_a_worker_s_lines_while_flushing():
    msgs = []
    first_done = asyncio.Event()

    async def report(text):
        if text == "w1 held":  # the flusher is mid-send of w1's held line...
            await asyncio.sleep(0.02)
        msgs.append(text)

    async def work(session, report):
        if session == 0:
            await asyncio.sleep(0.01)
            first_done.set()
            return
        await report("w1 held")  # held: worker 0 is still running
        await first_done.wait()
        await asyncio.sleep(0.015)  # ...when w1 reports again
        await report("w1 next")

    asyncio.run(_fn([0, 1]).gather_in_order(work, report))

    assert msgs == ["w1 held", "w1 next"]
