"""Offline tests for the aiogram control-panel bot and the decoupled function cores.

Async paths are driven with asyncio.run() to match tests/test_functions.py (the
project does not use pytest-asyncio).
"""

import asyncio
import contextlib
import types

import pytest
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

from bot.callbacks import ChoiceCB, FunctionCB, MenuCB
from bot.config import BotConfig
from bot.middlewares.auth import AuthMiddleware
from bot.services.delegation import HostActionBlocked, WorkerPool
from bot.services.registry import BOT_FUNCTIONS_BY_KEY, RISKY, SAFE


def _client():
    """A real but unconnected Telethon client (a valid worker instance)."""
    return TelegramClient(StringSession(), 1, "x")


class _Pool(WorkerPool):
    def __init__(self, workers):
        self._workers = list(workers)

    @property
    def workers(self):
        return list(self._workers)


# --- callback data factories ------------------------------------------------

class TestCallbacks:
    def test_function_roundtrip(self):
        assert FunctionCB.unpack(FunctionCB(key="pm").pack()).key == "pm"

    def test_choice_roundtrip(self):
        cb = ChoiceCB.unpack(ChoiceCB(scope="pm_mode", value="phone").pack())
        assert (cb.scope, cb.value) == ("pm_mode", "phone")

    def test_menu_roundtrip(self):
        assert MenuCB.unpack(MenuCB(action="home").pack()).action == "home"


# --- auth middleware (whitelist) -------------------------------------------

class TestAuth:
    def test_admin_passes(self):
        mw = AuthMiddleware({42})
        called = []

        async def handler(event, data):
            called.append(True)
            return "ok"

        data = {"event_from_user": types.SimpleNamespace(id=42)}
        assert asyncio.run(mw(handler, object(), data)) == "ok"
        assert called == [True]

    def test_non_admin_dropped(self):
        mw = AuthMiddleware({42})
        called = []

        async def handler(event, data):
            called.append(True)

        data = {"event_from_user": types.SimpleNamespace(id=7)}
        assert asyncio.run(mw(handler, object(), data)) is None
        assert called == []

    def test_no_user_dropped(self):
        mw = AuthMiddleware({42})

        async def handler(event, data):
            return "ok"

        assert asyncio.run(mw(handler, object(), {})) is None


# --- worker pool / host protection -----------------------------------------

class TestWorkerPool:
    def test_risky_without_workers_refuses(self):
        pool = _Pool([])
        bf = BOT_FUNCTIONS_BY_KEY["pm"]
        assert bf.risk == RISKY

        messages = []
        ran = []

        async def report(text):
            messages.append(text)

        async def factory(f):
            ran.append(True)

        asyncio.run(pool.run(types.SimpleNamespace(), bf, factory, report))

        assert ran == []
        assert any("воркер" in m.lower() for m in messages)

    def test_assert_workers_rejects_non_client(self):
        with pytest.raises(HostActionBlocked):
            WorkerPool._assert_workers([object()])

    def test_delegates_workers_to_instance(self):
        workers = [_client()]
        pool = _Pool(workers)
        bf = BOT_FUNCTIONS_BY_KEY["status"]
        assert bf.risk == SAFE

        instance = types.SimpleNamespace(sessions=None)
        ran = []

        async def factory(f):
            ran.append(f.sessions)

        asyncio.run(pool.run(instance, bf, factory, report=None))

        assert instance.sessions == workers
        assert ran == [workers]


# --- config -----------------------------------------------------------------

class TestBotConfig:
    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("BOT_TOKEN", "123:abc")
        monkeypatch.setenv("BOT_ADMINS", "1, 2 3")
        cfg = BotConfig(path="does-not-exist.toml")
        assert cfg.token == "123:abc"
        assert cfg.admins == {1, 2, 3}

    def test_validate_empty_exits(self, monkeypatch):
        monkeypatch.delenv("BOT_TOKEN", raising=False)
        monkeypatch.delenv("BOT_ADMINS", raising=False)
        cfg = BotConfig(path="does-not-exist.toml")
        with pytest.raises(SystemExit):
            cfg.validate()

    def test_token_in_toml_is_rejected(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text('[bot]\ntoken = "123:abc"\nadmins = [1]\n')
        with pytest.raises(SystemExit, match="BOT_TOKEN"):
            BotConfig(path=str(path))


# --- decoupled function cores: run() routes output through the reporter -----

class _Storage:
    initialize = False
    sessions = []  # read by TelethonFunction.__init__; tests set fn.sessions after

    @contextlib.asynccontextmanager
    async def ainitialize_session(self, session):
        yield


class _JoinSession:
    """A fake Telethon client: calling it records the request and succeeds."""

    def __init__(self):
        self.requests = []

    def __call__(self, request):
        async def _c():
            self.requests.append(request)
            return "ok"
        return _c()


class TestFunctionCores:
    def test_joiner_run_reports_and_delegates(self):
        from functions.joiner import JoinerFunc

        fn = JoinerFunc(_Storage(), types.SimpleNamespace(delay=[0]))
        fn.sessions = [_JoinSession()]

        messages = []

        async def report(text):
            messages.append(text)

        asyncio.run(fn.run("1", "@channel", [0], report))

        assert fn.sessions[0].requests, "join request should have been sent on the worker"
        assert any("joined" in m for m in messages)

    def test_inviting_run_reports_when_no_targets(self):
        from functions.inviting import InvitingFunc

        fn = InvitingFunc(_Storage(), types.SimpleNamespace(delay=[0]))
        fn.sessions = []  # nothing to parse with

        messages = []

        async def report(text):
            messages.append(text)

        asyncio.run(fn.run("@src", "@dst", [0], report))

        assert any("parse" in m.lower() for m in messages)
