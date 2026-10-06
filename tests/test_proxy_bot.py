"""Offline tests for the bot proxy-setup handler (bot/routers/accounts.py)."""

import asyncio
import types

from bot.routers import accounts


def ns(**kw):
    return types.SimpleNamespace(**kw)


class _State:
    def __init__(self):
        self.cleared = False

    async def clear(self):
        self.cleared = True


class _Msg:
    def __init__(self, text=None, document=None):
        self.text = text
        self.document = document
        self.replies = []

    async def answer(self, text, **kwargs):
        self.replies.append(text)


class _Pool:
    def __init__(self, storage, polling=None):
        self.storage = storage
        self.polling = polling  # the worker autoreply is polling now (WorkerPool.polling)


class _Storage:
    def __init__(self):
        self.applied = None

    def apply_proxies(self, proxies):
        self.applied = proxies
        return {"accounts": 6, "proxies_used": 2, "string_sessions_skipped": 0}


def test_text_proxies_applied_and_file_written(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "assets").mkdir()

    storage = _Storage()
    msg = _Msg(text="socks5://u:p@1.1.1.1:1080\nhttp://2.2.2.2:3128")
    state = _State()

    asyncio.run(accounts.proxy_apply(msg, state, _Pool(storage), ns(active=False, label="")))

    assert len(storage.applied) == 2
    assert state.cleared is True
    assert (tmp_path / "assets" / "proxies.txt").read_text().strip().splitlines() == [
        "socks5://u:p@1.1.1.1:1080",
        "http://2.2.2.2:3128",
    ]
    assert any("применены" in r for r in msg.replies)


def test_refuses_while_job_active(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "assets").mkdir()

    storage = _Storage()
    msg = _Msg(text="socks5://u:p@1.1.1.1:1080")
    state = _State()

    asyncio.run(accounts.proxy_apply(msg, state, _Pool(storage), ns(active=True, label="Рассылка")))

    assert storage.applied is None  # nothing applied
    assert state.cleared is False
    assert not (tmp_path / "assets" / "proxies.txt").exists()


def test_bad_proxy_line_reports(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "assets").mkdir()

    storage = _Storage()
    msg = _Msg(text="garbage-line")
    state = _State()

    asyncio.run(accounts.proxy_apply(msg, state, _Pool(storage), ns(active=False, label="")))

    assert storage.applied is None
    assert state.cleared is False
    assert any("разобрать" in r for r in msg.replies)


def test_refuses_while_autoreply_polls(tmp_path, monkeypatch):
    # the polled worker's old client stays connected through its old proxy until the poll ends
    monkeypatch.chdir(tmp_path)
    (tmp_path / "assets").mkdir()

    storage = _Storage()
    msg = _Msg(text="socks5://u:p@1.1.1.1:1080")
    state = _State()

    pool = _Pool(storage, polling="sessions/a.jsession")
    asyncio.run(accounts.proxy_apply(msg, state, pool, ns(active=False, label="")))

    assert storage.applied is None
    assert state.cleared is False
    assert any("автоответа" in r for r in msg.replies)
