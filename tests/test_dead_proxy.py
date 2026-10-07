"""A worker whose proxy is dead (connect() fails) is skipped; the job goes on with the others.

In the bot (initialize=False) SessionsStorage.ainitialize_session connects each worker; a
failure there used to raise past every function's per-worker get_me guard and end the job.
"""

import asyncio
import types

from functions.changebio import ChangeBioFunc
from functions.clear_chats import ClearDialogsFunc
from functions.pmmailing import PmMailingFunc
import functions.pmmailing as pm
from modules.rich_message import RichContent
from modules.storages.sessions_storage import SessionsStorage


class _Worker:
    def __init__(self, name, dead=False):
        self.name, self.dead = name, dead
        self.connected = False
        self.requests = []
        self.session = types.SimpleNamespace(_entities=set())

    async def connect(self):
        if self.dead:
            raise ConnectionError("Connection to Telegram failed 5 time(s)")
        self.connected = True

    async def disconnect(self):
        self.connected = False

    def _check(self):
        if not self.connected:  # as Telethon after a failed connect()
            raise ConnectionError("Cannot send requests while disconnected")

    async def get_me(self):
        self._check()
        return types.SimpleNamespace(id=hash(self.name) % 1000, first_name=self.name, username=None)

    async def __call__(self, request):
        self._check()
        self.requests.append(request)

    async def iter_dialogs(self):
        self._check()
        return
        yield

    async def send_message(self, peer, text, **kw):
        self._check()
        self.requests.append(("msg", peer))


def _storage(tmp_path):
    return SessionsStorage(str(tmp_path), 1, "x", initialize=False)


def _messages():
    out = []

    async def report(text):
        out.append(text)
    return out, report


def test_failed_connect_does_not_raise(tmp_path):
    storage, dead = _storage(tmp_path), _Worker("dead", dead=True)

    async def use():
        async with storage.ainitialize_session(dead):
            return "inside"

    assert asyncio.run(use()) == "inside"


def test_profile_job_skips_the_dead_worker(tmp_path):
    dead, ok = _Worker("dead", dead=True), _Worker("ok")
    fn = ChangeBioFunc(_storage(tmp_path), types.SimpleNamespace(profile_pause=[0]))
    fn.sessions = [dead, ok]
    msgs, report = _messages()

    asyncio.run(fn.run(report, bio="hi"))  # must not raise

    assert len(ok.requests) == 1 and dead.requests == []
    assert any("не удалось опросить аккаунт" in m for m in msgs) and any("bio изменено" in m for m in msgs)


def test_clear_dialogs_skips_the_dead_worker(tmp_path):
    dead, ok = _Worker("dead", dead=True), _Worker("ok")
    fn = ClearDialogsFunc(_storage(tmp_path), types.SimpleNamespace())
    fn.sessions = [dead, ok]
    msgs, report = _messages()

    asyncio.run(fn.run(report))  # must not raise

    assert any("не удалось опросить аккаунт" in m for m in msgs)


def test_mailing_rotates_past_the_dead_worker(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(pm, "STATS_PATH", str(tmp_path / "pm.json"))
    monkeypatch.setattr(pm, "LIMITS_PATH", str(tmp_path / "limits.json"))
    dead, ok = _Worker("dead", dead=True), _Worker("ok")
    settings = types.SimpleNamespace(delay=[0], per_account_daily=0, account_pause=[0])
    fn = PmMailingFunc(_storage(tmp_path), settings)
    fn.sessions = [dead, ok]

    async def no_check(report):  # the @SpamBot pre-check is not under test here
        pass

    monkeypatch.setattr(fn, "check_workers", no_check)
    msgs, report = _messages()

    asyncio.run(fn.run(["alice", "bob"], RichContent(text="hi"), [0], report))

    assert [peer for _, peer in ok.requests] == ["alice", "bob"]
