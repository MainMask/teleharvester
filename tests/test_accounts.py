"""Offline tests for the bot accounts list (bot/routers/accounts.py)."""

import asyncio
import contextlib
import types

from bot.routers import accounts


def ns(**kw):
    return types.SimpleNamespace(**kw)


class _Msg:
    def __init__(self):
        self.replies = []

    async def answer(self, text, **kwargs):
        self.replies.append(text)


class _Client:
    def __init__(self, me=None, fail=False):
        self._me = me
        self._fail = fail

    async def get_me(self):
        if self._fail:
            raise RuntimeError("boom")
        return self._me


class _Storage:
    def __init__(self, jsessions=None):
        self.jsessions_paths = jsessions or {}

    @contextlib.asynccontextmanager
    async def ainitialize_session(self, client):
        yield

    def get_session_path(self, client):
        return getattr(client, "path", None)


class _Pool:
    def __init__(self, workers, storage):
        self._workers = workers
        self.storage = storage

    @property
    def workers(self):
        return self._workers

    def count(self):
        return len(self._workers)


class _Manager:
    def __init__(self, free=True):
        self._free = free
        self.label = "Рассылка"
        self.released = False

    def acquire(self, label, cancelable=True, timeout=600):
        return self._free

    def release(self):
        self.released = True


def test_lists_each_account(tmp_path):
    workers = [
        _Client(ns(first_name="Meggan", last_name="Page", id=111, username="meg")),
        _Client(ns(first_name="Ivan", last_name=None, id=222, username=None)),
    ]
    pool = _Pool(workers, _Storage())
    manager = _Manager(free=True)
    msg = _Msg()

    asyncio.run(accounts.accounts(msg, pool, manager))

    joined = "\n".join(msg.replies)
    assert "всего: <b>2</b>" in joined
    assert "1. Meggan Page — @meg — ID 111" in joined
    assert "2. Ivan — — — ID 222" in joined
    assert manager.released is True


def test_busy_shows_only_count(tmp_path):
    pool = _Pool([_Client(ns(first_name="A", last_name=None, id=1, username=None))], _Storage())
    manager = _Manager(free=False)
    msg = _Msg()

    asyncio.run(accounts.accounts(msg, pool, manager))

    joined = "\n".join(msg.replies)
    assert "всего: <b>1</b>" in joined
    assert "Идёт задача" in joined
    assert "ID 1" not in joined  # no per-account details while busy


def test_failed_worker_falls_back_to_stored(tmp_path):
    client = _Client(fail=True)
    client.path = "sessions/x.jsession"
    stored = ns(account=ns(account=ns(first_name="Stored", last_name="Name",
                                       phone_number="79990001122", user_id=333)))
    pool = _Pool([client], _Storage({"sessions/x.jsession": stored}))
    manager = _Manager(free=True)
    msg = _Msg()

    asyncio.run(accounts.accounts(msg, pool, manager))

    joined = "\n".join(msg.replies)
    assert "Stored Name" in joined
    assert "+79990001122" in joined
    assert "не удалось опросить" in joined
    assert manager.released is True


def test_empty_pool(tmp_path):
    pool = _Pool([], _Storage())
    manager = _Manager(free=True)
    msg = _Msg()

    asyncio.run(accounts.accounts(msg, pool, manager))

    assert any("Воркеров: <b>0</b>" in r for r in msg.replies)


def test_names_are_html_escaped(tmp_path):
    workers = [_Client(ns(first_name="A&lt;B", last_name="<i>", id=1, username=None))]
    pool = _Pool(workers, _Storage())
    msg = _Msg()

    asyncio.run(accounts.accounts(msg, pool, _Manager(free=True)))

    joined = "\n".join(msg.replies)
    assert "A&amp;lt;B &lt;i&gt;" in joined


def test_slow_connect_is_timed_out(monkeypatch, tmp_path):
    class _SlowStorage(_Storage):
        @contextlib.asynccontextmanager
        async def ainitialize_session(self, client):
            await asyncio.sleep(10)  # a dead proxy: connect() hangs
            yield

    monkeypatch.setattr(accounts, "GET_ME_TIMEOUT", 0.05)
    pool = _Pool([_Client(ns(first_name="A", last_name=None, id=1, username=None))], _SlowStorage())
    manager = _Manager(free=True)
    msg = _Msg()

    asyncio.run(asyncio.wait_for(accounts.accounts(msg, pool, manager), 2))

    assert "не удалось опросить" in "\n".join(msg.replies)
    assert manager.released is True
