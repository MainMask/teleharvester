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

from bot.callbacks import ChoiceCB, FunctionCB, MenuAction, MenuCB
from bot.config import BotConfig
from bot.middlewares.auth import AuthMiddleware
from bot.services.delegation import HostActionBlocked, WorkerPool
from bot.services.registry import BOT_FUNCTIONS_BY_KEY, RISKY, SAFE


def _client():
    """A real but unconnected Telethon client (a valid worker instance)."""
    return TelegramClient(StringSession(), 1, "x")


class _Pool(WorkerPool):
    def __init__(self, workers):
        super().__init__(None)
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
        assert MenuCB.unpack(MenuCB(action=MenuAction.WORKERS).pack()).action == MenuAction.WORKERS


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

    @staticmethod
    def _pool_of(*paths):
        clients = [_client() for _ in paths]
        by_id = dict(zip(map(id, clients), paths))
        storage = types.SimpleNamespace(sessions=clients, get_session_path=lambda c: by_id.get(id(c)))
        return WorkerPool(storage), clients

    def test_a_scraping_worker_is_left_out_of_jobs(self):
        pool, (scraping, other) = self._pool_of("sessions/a.jsession", "sessions/b.jsession")
        pool.scraping = types.SimpleNamespace(path="sessions/a.jsession", label="Ann (@a)")
        instance, messages, seen = types.SimpleNamespace(sessions=None), [], []

        async def report(text):
            messages.append(text)

        async def factory(f):
            seen.append((f.sessions, list(pool.in_job)))

        assert asyncio.run(pool.run(instance, BOT_FUNCTIONS_BY_KEY["pm"], factory, report)) is True
        assert seen == [([other], [other])] and pool.in_job == []
        assert instance.on_hold == [scraping]  # on hold, not gone: a mailing keeps its people for it
        assert messages == ["ℹ️ Воркер Ann (@a) занят скрапом — задача идёт без него."]

    def test_a_risky_job_waits_when_the_only_worker_is_scraping(self):
        pool, _ = self._pool_of("sessions/a.jsession")
        pool.scraping = types.SimpleNamespace(path="sessions/a.jsession", label="Ann (@a)")
        messages, ran = [], []

        async def report(text):
            messages.append(text)

        async def factory(f):
            ran.append(True)

        assert asyncio.run(pool.run(types.SimpleNamespace(), BOT_FUNCTIONS_BY_KEY["pm"], factory, report)) is False
        assert ran == [] and "занят скрапом" in messages[0]

    def test_a_worker_is_busy_while_its_job_runs_and_not_after(self):
        pool, _ = self._pool_of("sessions/a.jsession")
        during = []

        async def factory(f):
            during.append(pool.busy("sessions/a.jsession"))
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            asyncio.run(pool.run(types.SimpleNamespace(), BOT_FUNCTIONS_BY_KEY["status"], factory, report=None))
        assert during == [True] and not pool.busy("sessions/a.jsession")

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

    def test_non_numeric_admin_exits_with_a_message(self, monkeypatch):
        monkeypatch.setenv("BOT_ADMINS", "1,abc")
        with pytest.raises(SystemExit, match="числовыми"):
            BotConfig(path="does-not-exist.toml")

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
        assert any("вступил" in m for m in messages)

    def test_joiner_waits_between_accounts_not_after_the_last(self):
        from functions.joiner import JoinerFunc

        fn = JoinerFunc(_Storage(), types.SimpleNamespace(delay=[0]))
        fn.sessions = [_JoinSession(), _JoinSession(), _JoinSession()]
        delays = []

        async def delay():
            delays.append(1)

        async def report(text):
            pass

        fn.delay = delay
        asyncio.run(fn.run("1", "@channel", [0], report))
        assert len(delays) == 2

    def test_inviting_run_reports_when_no_targets(self):
        from functions.inviting import InvitingFunc

        fn = InvitingFunc(_Storage(), types.SimpleNamespace(delay=[0], invite_per_account_daily=0))
        fn.sessions = []  # nothing to parse with

        messages = []

        async def report(text):
            messages.append(text)

        asyncio.run(fn.run("@src", "@dst", [0], report))

        assert any("исходного чата" in m for m in messages)


# --- start: waiting for the Bot API -----------------------------------------

class TestWaitForBotApi:
    @staticmethod
    def _bot(*outcomes):
        calls = []

        async def me():
            calls.append(True)
            outcome = outcomes[len(calls) - 1]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        return types.SimpleNamespace(me=me), calls

    def test_network_error_is_retried_until_reachable(self):
        from aiogram.exceptions import TelegramNetworkError
        from bot.app import wait_for_bot_api

        bot, calls = self._bot(TelegramNetworkError(None, "down"), "me")
        asyncio.run(wait_for_bot_api(bot, delay=0))
        assert len(calls) == 2

    def test_bad_token_still_exits(self):
        from aiogram.exceptions import TelegramUnauthorizedError
        from bot.app import wait_for_bot_api

        bot, _ = self._bot(TelegramUnauthorizedError(None, "Unauthorized"))
        with pytest.raises(TelegramUnauthorizedError):
            asyncio.run(wait_for_bot_api(bot, delay=0))
