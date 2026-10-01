"""Offline tests for the phase-2 bot coverage: registry, decoupled run()s,
background tasks, the report stepper and scraper helpers."""

import asyncio
import contextlib
import types

from telethon import types as tl
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

from bot.services.registry import (
    BOT_FUNCTIONS,
    BOT_FUNCTIONS_BY_KEY,
    RISKY,
    SAFE,
    by_category,
    categories,
)
from bot.services.jobs import JobManager


def ns(**kw):
    return types.SimpleNamespace(**kw)


class _Storage:
    initialize = False
    sessions = []

    @contextlib.asynccontextmanager
    async def ainitialize_session(self, session):
        yield


class _Session:
    """Fake Telethon client: awaitable calls, get_me, send_message all succeed."""

    def __init__(self, name="A"):
        self.name = name
        self.requests = []

    async def get_me(self):
        return ns(first_name=self.name, last_name=None, id=1, phone="12025550123")

    def __call__(self, request):
        async def _c():
            self.requests.append(request)
            return "ok"
        return _c()

    async def send_message(self, *a, **k):
        self.requests.append(("send_message", a, k))
        return "ok"


def collect():
    msgs = []

    async def report(text):
        msgs.append(text)

    return msgs, report


# --- registry ---

class TestRegistry:
    def test_keys_unique(self):
        assert len(BOT_FUNCTIONS_BY_KEY) == len(BOT_FUNCTIONS)

    def test_categories_cover_all(self):
        total = sum(len(by_category(c)) for c in categories())
        assert total == len(BOT_FUNCTIONS)

    def test_risk_values_valid(self):
        assert all(f.risk in (SAFE, RISKY) for f in BOT_FUNCTIONS)


# --- decoupled run()s route output through the reporter ---

class TestRuns:
    def test_changebio(self):
        from functions.changebio import ChangeBioFunc
        fn = ChangeBioFunc(_Storage(), ns(delay=[0]))
        fn.sessions = [_Session()]
        msgs, report = collect()
        asyncio.run(fn.run("hello", report))
        assert any("bio changed" in m for m in msgs)

    def test_reactions_parses_link(self):
        from functions.reactions import ReactionsFunc
        fn = ReactionsFunc(_Storage(), ns(delay=[0]))
        fn.sessions = [_Session()]
        msgs, report = collect()
        asyncio.run(fn.run("t.me/chan/5", "🔥", report))
        assert any("Reaction" in m for m in msgs)

    def test_report_user(self):
        from functions.report_user import ReportUserFunc
        fn = ReportUserFunc(_Storage(), ns())
        fn.sessions = [_Session()]
        msgs, report = collect()
        reason = fn.reasons[0][1]
        asyncio.run(fn.run("@someone", reason, "spam", report))
        assert any("submitted" in m for m in msgs)

    def test_broadcast_comments(self):
        from functions.broadcast_comments import CommentsBroadcastFunc
        fn = CommentsBroadcastFunc(_Storage(), ns(delay=[0], messages_count=1))
        fn.sessions = [_Session()]
        msgs, report = collect()
        asyncio.run(fn.run("t.me/chan/5", False, ["hi"], [0], report))
        assert any("sent" in m for m in msgs)

    def test_statistics_tally(self):
        from functions.statistics_phones import PhoneNumbersStatsFunc
        fn = PhoneNumbersStatsFunc(_Storage(), ns())
        rows = fn.tally(["12025550123", "12025550124"])
        assert rows and rows[0][0] == 1 and rows[0][2] == 2


# --- job manager (single-slot, stop, exclusivity) ---

class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append(text)
        return ns(message_id=1)

    async def edit_message_text(self, *a, **k):
        return None


class _Pool:
    def __init__(self, workers=()):
        self._w = list(workers)

    @property
    def workers(self):
        return self._w

    async def run(self, instance, bot_function, factory, report):
        await factory(instance)


class TestJobManager:
    def test_second_run_refused_while_busy(self):
        async def scenario():
            m = JobManager()
            bot = _Bot()
            started = asyncio.Event()
            release = asyncio.Event()

            async def job(f, r):
                started.set()
                await release.wait()

            ok1 = await m.run(bot, 1, _Pool(), object(), ns(risk="safe"), job, "H1", "done")
            await started.wait()
            ok2 = await m.run(bot, 1, _Pool(), object(), ns(risk="safe"),
                              lambda f, r: asyncio.sleep(0), "H2", "done")
            release.set()
            await asyncio.sleep(0.05)
            return ok1, ok2, m.active

        ok1, ok2, active = asyncio.run(scenario())
        assert ok1 is True and ok2 is False and active is False

    def test_stop_cancels_and_disconnects(self):
        class S:
            def __init__(self):
                self.disconnected = False

            async def disconnect(self):
                self.disconnected = True

        async def scenario():
            m = JobManager()
            bot = _Bot()
            s = S()
            started = asyncio.Event()

            async def job(f, r):
                started.set()
                await asyncio.sleep(3600)

            await m.run(bot, 1, _Pool([s]), object(), ns(risk="safe"), job, "H", "done", stop_sessions=[s])
            await started.wait()
            stopped = await m.stop()
            await asyncio.sleep(0.05)
            return stopped, s.disconnected, m.active

        stopped, disconnected, active = asyncio.run(scenario())
        assert stopped is True and disconnected is True and active is False

    def test_acquire_is_exclusive(self):
        m = JobManager()
        assert m.acquire("A", timeout=0) is True
        assert m.acquire("B", timeout=0) is False
        m.release()
        assert m.acquire("C", timeout=0) is True

    def test_stop_interactive_calls_on_abort(self):
        called = []
        m = JobManager()
        m.acquire("X", on_abort=lambda: called.append(True), timeout=0)
        stopped = asyncio.run(m.stop())
        assert stopped is True and called == [True] and m.active is False

    def test_non_cancelable_stop_refused(self):
        m = JobManager()
        m.acquire("Scrape", cancelable=False, timeout=0)
        assert asyncio.run(m.stop()) is False
        assert m.active is True


# --- report (message) dynamic stepper ---

class TestReportStepper:
    def test_choose_then_done(self):
        from functions.report import ReportFunc
        fn = ReportFunc(_Storage(), ns())

        option = ns(text="Spam", option=b"1")
        results = iter([
            tl.ReportResultChooseOption(title="why", options=[option]),
            tl.ReportResultReported(),
        ])

        async def fake_step(session, peer, ids, comment, opt):
            return next(results)

        fn.report_step = fake_step
        flow = {"session": object(), "peer": "x", "ids": [1], "comment": "c", "selections": []}

        status, options = asyncio.run(fn.step(flow, b""))
        assert status == "choose" and len(options) == 1

        status2, _ = asyncio.run(fn.step(flow, options[0].option))
        assert status2 == "done"

    def test_replay_rest_reports(self):
        from functions.report import ReportFunc
        fn = ReportFunc(_Storage(), ns())

        called = []

        async def fake_replay(session, peer, ids, comment, selections):
            called.append(session)

        fn.replay = fake_replay
        msgs, report = collect()
        asyncio.run(fn.replay_rest([_Session()], "x", [1], "c", [0], report))

        assert called and any("submitted" in m for m in msgs)


# --- trigger-listener cleans up its event handler ---

class _ListenSession:
    def __init__(self):
        self.added = []
        self.removed = []

    def add_event_handler(self, handler, event):
        self.added.append((handler, event))

    def remove_event_handler(self, handler, event):
        self.removed.append((handler, event))

    async def connect(self):
        return None

    async def run_until_disconnected(self):
        return None


class TestBroadcastHandleCleanup:
    def test_handler_removed_after_listener_ends(self):
        from functions.broadcast import Broadcast

        b = Broadcast(_Storage(), ns(trigger="go", messages=["m"], delay=[0], messages_count=1))
        session = _ListenSession()

        async def report(_text):
            return None

        asyncio.run(b.handle(session, lambda *a: None, report))

        assert len(session.added) == 1
        assert len(session.removed) == 1
        assert session.added[0][0] is session.removed[0][0]  # same handler added and removed


# --- scraper helpers ---

class TestScraping:
    def test_parse_channels(self):
        from bot.services import scraping
        assert scraping.parse_channels("a, b  c") == ["a", "b", "c"]

    def test_read_preview_escapes_html(self):
        import pandas as pd
        from bot.routers.scraping import read_preview

        df = pd.DataFrame({"Content": ["check <b>x</b> & http://y", "a < b"]})
        out = read_preview(df)

        # the raw content's '<' and '&' must be escaped so Telegram HTML parsing won't 400
        assert "<b>x</b>" not in out
        assert "&lt;" in out and "&amp;" in out
        assert out.startswith("<pre>") and "</pre>" in out

    def test_read_preview_truncates(self):
        import pandas as pd
        from bot.routers.scraping import read_preview

        df = pd.DataFrame({"Content": ["x" * 10000]})
        out = read_preview(df)
        assert "…" in out and len(out) < 4096

    def test_session_string_none_without_workers(self):
        from bot.services import scraping
        assert scraping.worker_session_string(ns(workers=[])) is None

    def test_session_string_from_worker(self):
        from bot.services import scraping
        client = TelegramClient(StringSession(), 1, "x")
        s = scraping.worker_session_string(ns(workers=[client]))
        assert s is not None and isinstance(s, str)  # real sessions serialise a non-empty key
