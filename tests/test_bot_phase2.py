"""Offline tests for the phase-2 bot coverage: registry, decoupled run()s,
background tasks, the report stepper and scraper helpers."""

import asyncio
import contextlib
import os
import types

from telethon import types as tl
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

from bot.services.registry import (
    BOT_FUNCTIONS,
    BOT_FUNCTIONS_BY_KEY,
    RISKY,
    SAFE,
    SECTIONS,
    WORKER_GROUPS,
    by_category,
)
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager


def ns(**kw):
    return types.SimpleNamespace(**kw)


class _Storage:
    initialize = False
    sessions = []

    @contextlib.asynccontextmanager
    async def ainitialize_session(self, session):
        yield

    def get_session_path(self, session):
        return None

    def remember_name(self, session, first_name, last_name):
        pass


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
        total = sum(len(by_category(c)) for c in (*SECTIONS, *WORKER_GROUPS))
        assert total == len(BOT_FUNCTIONS)

    def test_every_category_is_reachable_from_the_menu(self):
        # a category outside SECTIONS / WORKER_GROUPS would have no button leading to it
        assert {f.category for f in BOT_FUNCTIONS} == set(SECTIONS) | set(WORKER_GROUPS)

    def test_risk_values_valid(self):
        assert all(f.risk in (SAFE, RISKY) for f in BOT_FUNCTIONS)


# --- decoupled run()s route output through the reporter ---

class TestRuns:
    def test_changebio(self):
        from functions.changebio import ChangeBioFunc
        fn = ChangeBioFunc(_Storage(), ns(delay=[0], profile_pause=[0]))
        fn.sessions = [_Session()]
        msgs, report = collect()
        asyncio.run(fn.run(report, bio="hello"))
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
        fn = ReportUserFunc(_Storage(), ns(delay=[0]))
        fn.sessions = [_Session()]
        msgs, report = collect()
        reason = fn.reasons[0][1]
        asyncio.run(fn.run("@someone", reason, "spam", report))
        assert any("submitted" in m for m in msgs)

    def test_broadcast_comments(self):
        from functions.broadcast_comments import CommentsBroadcastFunc
        from modules.rich_message import RichContent
        fn = CommentsBroadcastFunc(_Storage(), ns(delay=[0], messages_count=1))
        fn.sessions = [_Session()]
        msgs, report = collect()
        asyncio.run(fn.run("t.me/chan/5", RichContent(text="hi"), [0], report))
        assert any("sent" in m for m in msgs)

    def test_statistics_tally(self):
        from functions.statistics_phones import PhoneNumbersStatsFunc
        fn = PhoneNumbersStatsFunc(_Storage(), ns())
        rows = fn.tally(["12025550123", "12025550124"])
        assert rows and rows[0][0] == 1 and rows[0][2] == 2

    def test_statistics_count_a_worker_on_hold_by_its_stored_number(self):
        from functions.statistics_phones import PhoneNumbersStatsFunc

        class _Worker:
            async def get_me(self):
                return ns(phone="12025550123")

        held = object()  # busy scraping: not asked, its .jsession has the number
        storage = _Storage()
        storage.jsessions_paths = {"sessions/w.jsession": ns(account=ns(account=ns(phone_number="447700900123")))}
        storage.get_session_path = lambda s: "sessions/w.jsession" if s is held else None
        fn = PhoneNumbersStatsFunc(storage, ns())
        fn.sessions, fn.on_hold = [_Worker()], [held]
        msgs, report = collect()
        asyncio.run(fn.run(report))
        assert sorted((m.split(" — ")[0], m.split(" — ")[-1]) for m in msgs) == [("+1", "1"), ("+44", "1")]


class _DeadSession(_Session):
    """A worker whose session is dead/unauthorized: get_me raises."""

    async def get_me(self):
        raise RuntimeError("unauthorized")


class TestDeadWorkerSkipped:
    """A dead worker is skipped, not fatal to the whole batch."""

    def test_report_user_skips_dead_worker(self):
        from functions.report_user import ReportUserFunc
        fn = ReportUserFunc(_Storage(), ns(delay=[0]))
        fn.sessions = [_DeadSession(), _Session()]
        msgs, report = collect()
        asyncio.run(fn.run("@someone", fn.reasons[0][1], "spam", report))
        assert any("get_me failed" in m for m in msgs)  # dead one reported...
        assert any("submitted" in m for m in msgs)      # ...and the healthy one still ran

    def test_changename_survives_dead_worker(self):
        from functions.changename import ChangeNameFunc
        fn = ChangeNameFunc(_Storage(), ns(profile_pause=[0]))
        fn.sessions = [_DeadSession(), _Session()]
        msgs, report = collect()
        asyncio.run(fn.run(report, first_name="Ivan", last_name=None))  # must not raise
        assert any("get_me failed" in m for m in msgs)
        assert any("Name changed" in m for m in msgs)


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
        instance.sessions = self.workers  # as WorkerPool.delegate
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

            ok1 = await m.run(bot, 1, _Pool(), ns(), ns(risk="safe"), job, "H1", "done")
            await started.wait()
            ok2 = await m.run(bot, 1, _Pool(), ns(), ns(risk="safe"),
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

            await m.run(bot, 1, _Pool([s]), ns(), ns(risk="safe"), job, "H", "done", stop_sessions=[s])
            await started.wait()
            stopped = await m.stop()
            await asyncio.sleep(0.05)
            return stopped, s.disconnected, m.active

        stopped, disconnected, active = asyncio.run(scenario())
        assert stopped is True and disconnected is True and active is False

    def test_run_returns_false_when_status_message_fails(self):
        # reporter.start() can't post (chat unreachable): run frees the slot and reports
        # "not started" so the caller's not-started cleanup runs instead of leaking past a raise.
        class _DeadBot(_Bot):
            async def send_message(self, *a, **k):
                raise RuntimeError("chat unreachable")

        async def scenario():
            m = JobManager()
            started = await m.run(_DeadBot(), 1, _Pool(), ns(), ns(risk="safe"),
                                  lambda f, r: asyncio.sleep(0), "H", "done")
            return started, m.active

        started, active = asyncio.run(scenario())
        assert started is False and active is False

    def test_job_clears_worker_entity_cache(self):
        # A finished job drops each worker's Telethon in-memory entity set, so a
        # multi-day bot run doesn't accumulate every seen user/chat until restart.
        class S:
            def __init__(self):
                self.session = ns(_entities={("u", 1), ("u", 2)})

            async def disconnect(self):
                pass

        async def scenario():
            m = JobManager()
            s = S()
            await m.run(_Bot(), 1, _Pool([s]), ns(), ns(risk="safe"),
                        lambda f, r: asyncio.sleep(0), "H", "done", stop_sessions=[s])
            await asyncio.sleep(0.05)
            return s.session._entities

        assert asyncio.run(scenario()) == set()


class TestSessionRelease:
    def test_ainitialize_session_clears_entity_cache(self, tmp_path):
        # The shared release point for rotation/gather functions: on disconnect in bot
        # mode (initialize=False) the worker's growing _entities set is dropped too.
        from modules.storages.sessions_storage import SessionsStorage

        storage = SessionsStorage(str(tmp_path), 1, "hash", initialize=False)

        class S:
            def __init__(self):
                self.session = ns(_entities={("u", 1)})
                self.connected = False

            async def connect(self):
                self.connected = True

            async def disconnect(self):
                self.connected = False

        async def scenario():
            s = S()
            async with storage.ainitialize_session(s):
                s.session._entities.add(("u", 2))
            return s.session._entities, s.connected

        entities, connected = asyncio.run(scenario())
        assert entities == set() and connected is False

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
        fn = ReportFunc(_Storage(), ns(delay=[0]))

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

        asyncio.run(b.handle(session, report))

        assert len(session.added) == 1
        assert len(session.removed) == 1
        assert session.added[0][0] is session.removed[0][0]  # same handler added and removed


    def test_a_days_long_listener_keeps_the_entity_cache_bounded(self, monkeypatch):
        import functions.broadcast as bc
        from functions.broadcast import Broadcast

        monkeypatch.setattr(bc, "LISTENER_ENTITY_LIMIT", 3)
        b = Broadcast(_Storage(), ns(trigger="go", messages=["m"], delay=[0], messages_count=1))
        session = _ListenSession()
        session.session = ns(_entities={1, 2, 3})

        async def report(_text):
            return None

        asyncio.run(b.handle(session, report))
        handler = session.added[0][0]
        other = ns(raw_text="hello")  # not the trigger: nothing is sent

        asyncio.run(handler(other))
        assert session.session._entities == {1, 2, 3}  # at the limit: kept
        session.session._entities.add(4)
        asyncio.run(handler(other))
        assert session.session._entities == set()  # past it: dropped


class TestListenerSafety:
    def _broadcast(self, trigger="go"):
        from functions.broadcast import Broadcast

        return Broadcast(_Storage(), ns(trigger=trigger, messages=["m"], delay=[0], messages_count=1))

    def _handler(self, b):
        session = _ListenSession()
        session.session = ns(_entities=set())

        async def report(_text):
            return None

        asyncio.run(b.handle(session, report))
        return session

    def test_listens_to_incoming_messages_only(self):
        session = self._handler(self._broadcast())
        event = session.added[0][1]
        assert event.incoming is True  # a worker's own broadcast never triggers it again

    def test_an_empty_trigger_matches_nothing(self):
        b = self._broadcast(trigger="")
        sent = []

        async def broadcast(*a, **k):
            sent.append(a)

        b.broadcast = broadcast
        handler = self._handler(b).added[0][0]
        asyncio.run(handler(ns(raw_text="", reply_to=None, chat_id=1)))  # a photo without a caption
        assert sent == []

    def test_a_repeated_trigger_in_a_busy_chat_is_ignored(self):
        """Telethon runs handlers concurrently: a second trigger in a chat this worker is
        already broadcasting to must not start a second loop there (the rate would double)."""
        b = self._broadcast()
        started, release = [], asyncio.Event()

        async def broadcast(session, chat_id, report, reply_to=None):
            started.append(chat_id)
            await release.wait()

        b.broadcast = broadcast
        session = _ListenSession()
        session.session = ns(_entities=set())
        reports = []

        async def report(text):
            reports.append(text)

        asyncio.run(b.handle(session, report))
        handler = session.added[0][0]

        def trigger(chat_id):
            return handler(ns(raw_text="go", reply_to=None, chat_id=chat_id))

        async def scenario():
            first = asyncio.create_task(trigger(1))
            await asyncio.sleep(0)
            # same chat, still running: ignored (returns at once instead of a second loop)
            await asyncio.wait_for(trigger(1), 1)
            other = asyncio.create_task(trigger(2))  # another chat: its own loop
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(first, other)
            await trigger(1)                      # finished: a new trigger starts again

        asyncio.run(scenario())
        assert started == [1, 2, 1]
        # the operator is told once, for the one ignored trigger
        assert [r for r in reports if "повторный триггер" in r] == [
            "[worker] уже рассылаю в этот чат — повторный триггер пропущен"]

    def test_a_lost_connection_reconnects_and_goes_on(self, monkeypatch):
        import functions.broadcast as bc

        monkeypatch.setattr(bc, "LISTENER_RECONNECT_DELAY", 0)

        class Flaky(_ListenSession):
            runs = 0

            async def run_until_disconnected(self):
                type(self).runs += 1
                if type(self).runs < 3:  # Telethon's own reconnects ran out, twice
                    raise ConnectionError("Connection to Telegram failed 5 time(s)")

        b = self._broadcast()
        msgs = []

        async def report(text):
            msgs.append(text)

        session = Flaky()
        asyncio.run(b.handle(session, report))
        assert Flaky.runs == 3
        assert sum("reconnecting" in m for m in msgs) == 2
        assert len(session.removed) == 1

    def test_one_broken_worker_does_not_end_the_others(self):
        class LoggedOut(_ListenSession):
            async def run_until_disconnected(self):
                raise RuntimeError("AUTH_KEY_UNREGISTERED")

        b = self._broadcast()
        msgs = []

        async def report(text):
            msgs.append(text)

        async def both():  # as broadcast_chat runs them
            await asyncio.gather(b.handle(LoggedOut(), report), b.handle(_ListenSession(), report))

        asyncio.run(both())  # must not raise
        assert any("listener stopped: AUTH_KEY_UNREGISTERED" in m for m in msgs)


# --- report _finish always frees the slot (regression: stuck job on error) ---

class TestFinishReleasesSlot:
    def test_slot_freed_when_replay_raises(self):
        from bot.routers.moderation import _finish

        class _Inst:
            async def replay_rest(self, *a):
                raise RuntimeError("worker unreachable")

        class _Sess:
            async def disconnect(self):
                return None

        async def scenario():
            m = JobManager()
            m.acquire("Репорт", timeout=0)
            flow = {"session": _Sess(), "rest": [], "peer": "x",
                    "ids": [1], "comment": "c", "selections": []}
            try:
                await _finish(_Inst(), flow, _Bot(), 1, m, ns(in_job=[]))
            except RuntimeError:
                pass
            return m.active

        assert asyncio.run(scenario()) is False


# --- report _finish locks the slot so /cancel can't free it mid-replay (race), but a ⏹ /
# /cancel softly stops it: the rest of the accounts are skipped, the slot freed only at the end ---

class TestFinishLocksSlotDuringReplay:
    def test_cancel_during_replay_stops_softly(self):
        from bot.routers.moderation import _finish

        release = asyncio.Event()
        in_replay = asyncio.Event()
        aborted = []
        summaries = []

        class _Inst:
            async def replay_rest(self, *a):
                in_replay.set()
                await release.wait()  # replay is driving the rest of the workers right now
                return a[-1].is_set()  # the ⏹ reached it: the rest were skipped

        class _Sess:
            async def disconnect(self):
                return None

        class _ReportBot(_Bot):
            async def send_message(self, chat_id, text, **kw):
                summaries.append(text)
                return ns(message_id=1)

        async def scenario():
            m = JobManager()
            # on_abort would free the slot under the running replay: it must not fire
            m.acquire("Репорт", on_abort=lambda: aborted.append(True), timeout=0)
            flow = {"session": _Sess(), "rest": [], "peer": "x",
                    "ids": [1], "comment": "c", "selections": []}
            task = asyncio.create_task(_finish(_Inst(), flow, _ReportBot(), 1, m, ns(in_job=[])))
            await in_replay.wait()
            stopped = await m.stop()        # a /cancel mid-replay
            active_during = m.active
            release.set()
            await task
            return stopped, active_during, m.active

        stopped, active_during, active_after = asyncio.run(scenario())
        assert stopped is True          # accepted, as a soft stop
        assert active_during is True    # slot stayed held during replay_rest
        assert aborted == []            # on_abort didn't free it
        assert active_after is False    # freed once _finish returned
        assert any("⏹ Остановлено" in text for text in summaries)


class TestSoftStop:
    def test_locked_slot_without_soft_stop_refuses(self):
        async def scenario():
            m = JobManager()
            m.acquire("Анализ", cancelable=False, timeout=0)
            return await m.stop(), m.active

        assert asyncio.run(scenario()) == (False, True)

    def test_soft_stop_is_set_and_cleared_with_the_slot(self):
        async def scenario():
            m = JobManager()
            m.acquire("Репорт", timeout=0)
            m.lock()
            event = m.soft_stop()
            stopped, active = await m.stop(), m.active
            m.release()
            m.acquire("Следующая", cancelable=False, timeout=0)  # a later locked job: no stale event
            return stopped, active, event.is_set(), await m.stop()

        assert asyncio.run(scenario()) == (True, True, True, False)

    def test_replay_rest_skips_accounts_after_a_stop(self):
        from functions.report import ReportFunc

        stop = asyncio.Event()
        done = []

        @contextlib.asynccontextmanager
        async def ctx(session):
            yield

        class _Session:
            def __init__(self, name):
                self.name = name

            async def get_me(self):
                return ns(first_name=self.name)

        fn = ReportFunc(ns(sessions=[], ainitialize_session=ctx), ns(delay=[0]))

        async def replay(session, *a):
            done.append(session.name)
            stop.set()  # ⏹ pressed while the first remaining account reports

        fn.replay = replay

        async def report(text):
            pass

        skipped = asyncio.run(fn.replay_rest([_Session("a"), _Session("b")], "x", [1], "", [], report, stop))
        assert done == ["a"] and skipped is True

    def test_stop_after_the_last_account_is_not_reported_as_stopped(self):
        from bot.routers.moderation import _finish

        summaries = []

        class _Inst:
            async def replay_rest(self, *a):
                a[-1].set()  # ⏹ pressed once every account had reported
                return False

        class _Sess:
            async def disconnect(self):
                return None

        class _ReportBot(_Bot):
            async def send_message(self, chat_id, text, **kw):
                summaries.append(text)
                return ns(message_id=1)

        async def scenario():
            m = JobManager()
            m.acquire("Репорт", timeout=0)
            flow = {"session": _Sess(), "rest": [], "peer": "x",
                    "ids": [1], "comment": "c", "selections": []}
            await _finish(_Inst(), flow, _ReportBot(), 1, m, ns(in_job=[]))

        asyncio.run(scenario())
        assert any("Репорт отправлен" in t for t in summaries)
        assert not any("Остановлено" in t for t in summaries)


# --- mail_run bails before touching the shared instance when a job is running ---

class TestMailRunBusyGuard:
    def test_busy_does_not_touch_instance(self):
        from bot.routers.broadcasts import mail_run

        class _Inst:
            def __init__(self):
                self.touched = False

            def load_recipients(self, *a):
                self.touched = True
                return []

            def load_stats(self):
                self.touched = True

        class _State:
            cleared = False

            async def get_data(self):
                raise AssertionError("get_data must not run while busy")

            async def clear(self):
                self.cleared = True

        class _Msg:
            bot = _Bot()
            chat = ns(id=1)

            def __init__(self):
                self.replies = []

            async def answer(self, text, **kwargs):
                self.replies.append(text)

        inst = _Inst()
        m = JobManager()
        m.acquire("Другая задача", timeout=0)  # something else holds the slot
        msg = _Msg()

        asyncio.run(mail_run(msg, _State(), None, _Pool(), {"PmMailingFunc": inst},
                             m, ns(delay=[0])))

        assert any("Занят" in r for r in msg.replies)
        assert inst.touched is False  # the shared instance's stats were never reloaded


# --- stale inline buttons don't raise KeyError (reactions_run / ru_run) ---

class _EmptyState:
    async def get_data(self):
        return {}

    async def clear(self):
        return None


class TestStaleFlowGuards:
    def test_reactions_run_stale(self):
        from bot.routers.activity import reactions_run

        answered = []

        class _Cb:
            bot = _Bot()
            message = ns(chat=ns(id=1), answer=lambda t, **k: _append(answered, t))

            async def answer(self, *a, **k):
                return None

        async def scenario():
            m = JobManager()
            await reactions_run(_Cb(), ns(value="🔥"), _EmptyState(), _Pool(),
                                {}, m)  # empty state -> no data["link"]
            return m.active

        active = asyncio.run(scenario())
        assert active is False and any("устарел" in t for t in answered)

    def test_ru_run_stale(self):
        from bot.routers.moderation import ru_run

        class _Msg:
            text = "spam comment"
            bot = _Bot()
            chat = ns(id=1)

            def __init__(self):
                self.replies = []

            async def answer(self, text, **kwargs):
                self.replies.append(text)

        async def scenario():
            m = JobManager()
            msg = _Msg()
            await ru_run(msg, _EmptyState(), _Pool(), {}, m)  # no username/reason_index
            return msg.replies, m.active

        replies, active = asyncio.run(scenario())
        assert active is False and any("устарел" in r for r in replies)


def _append(bucket, text):
    bucket.append(text)
    return _noop_coro()


async def _noop_coro():
    return None


# --- mention_all tolerates a get_participants failure (sends without mentions) ---

class TestBroadcastMentionFailure:
    def test_participants_error_does_not_abort(self):
        from functions.broadcast import Broadcast
        from modules.rich_message import RichContent

        b = Broadcast(_Storage(), ns(trigger="go", messages=["m"], delay=[0], messages_count=1))
        b.configure(0, mention_all=True, mention_mode="admins", content=RichContent(text="hi"))

        class _S(_Session):
            async def get_participants(self, *a, **k):
                raise RuntimeError("no rights")

        msgs, report = collect()
        asyncio.run(b.broadcast(_S(), "peer", report))

        assert any("can't read participants" in m for m in msgs)
        assert any("sent" in m for m in msgs)  # the message still went out, just without mentions


# --- text-only FSM steps reject a non-text message instead of crashing ---

class TestRequireText:
    class _Msg:
        def __init__(self, text):
            self.text = text
            self.replies = []

        async def answer(self, text, **kwargs):
            self.replies.append(text)

    def test_rejects_non_text(self):
        from bot.routers._common import require_text
        m = self._Msg(None)  # e.g. a sticker/photo on a text step
        assert asyncio.run(require_text(m)) is None
        assert m.replies  # the operator is re-prompted

    def test_returns_stripped_text(self):
        from bot.routers._common import require_text
        m = self._Msg("  hello  ")
        assert asyncio.run(require_text(m)) == "hello"
        assert m.replies == []


# --- a bad (but textual) date is reported, not crashed (parse_date -> SystemExit) ---

class TestBadDateHandled:
    def test_scrape_run_reports_bad_date(self):
        from bot.routers import scraping

        class _State:
            async def get_data(self):
                return {"out_dir": "out", "channels": "@a", "name": "n", "date_min": "01.01.2024",
                        "account": "sessions/a.jsession"}

            async def clear(self):
                return None

        class _Msg:
            text = "not-a-date"
            chat = ns(id=1)
            bot = _Bot()

            def __init__(self):
                self.replies = []

            async def answer(self, text, **kwargs):
                self.replies.append(text)

        m = _Msg()
        asyncio.run(scraping.scrape_run(m, _State()))  # the date is checked before any slot

        assert any("Неверные параметры" in r and "not-a-date" in r for r in m.replies)


# --- verify: a user-input error (str SystemExit) surfaces its message, not "прервана" ---

class TestVerifyBadInputReported:
    def test_bad_input_shows_reason_and_frees_slot(self):
        from bot.services import scraping as svc
        from bot.routers import scraping

        class _State:
            async def get_data(self):
                return {"input": "nope.parquet", "channel": "@x", "date_min": "01.01.2024",
                        "account": "sessions/a.jsession"}

            async def clear(self):
                return None

        class _Msg:
            text = "02.01.2024"
            chat = ns(id=1)
            bot = _Bot()

            def __init__(self):
                self.replies = []

            async def answer(self, text, **kwargs):
                self.replies.append(text)

        async def _boom(creds, params):  # verify.run raises SystemExit(str) on bad input
            raise SystemExit("@x: channel not found")

        client = TelegramClient(StringSession(), 1, "x")
        m = _Msg()
        manager = JobManager()
        original = svc.do_verify
        svc.do_verify = _boom
        try:
            # one worker in the pool, as the scraper's account
            pool = WorkerPool(ns(sessions=[client], jsessions_paths={},
                                 get_session_path=lambda c: "sessions/a.jsession"))
            asyncio.run(scraping.verify_run(m, _State(), pool, None, manager))
        finally:
            svc.do_verify = original

        assert any("Неверные параметры" in r and "channel not found" in r for r in m.replies)
        assert not any("прервана" in r for r in m.replies)
        assert manager.active is False  # slot released on the error path


# --- 2fa: the operator's password message is deleted from the chat ---

class TestTwoFaDeletesPassword:
    def test_password_message_deleted_and_job_started(self):
        from bot.routers import profile

        class _State:
            async def clear(self):
                return None

        class _Msg:
            text = "s3cret"
            chat = ns(id=1)
            bot = _Bot()

            def __init__(self):
                self.deleted = False

            async def delete(self):
                self.deleted = True

        class _Manager:
            started = False

            async def run(self, *args, **kwargs):
                _Manager.started = True
                return True

        m = _Msg()
        asyncio.run(profile.twofa_run(m, _State(), ns(), {"SetPasswordFunc": object()}, _Manager()))

        assert m.deleted is True
        assert _Manager.started is True


# --- analysis: a bad input (SystemExit from scraper.analysis) is reported, not fatal ---

class TestAnalysisBadInputReported:
    def test_systemexit_is_reported_and_frees_slot(self, tmp_path):
        from bot.routers import scraping

        missing = str(tmp_path / "nope_*.parquet")  # combine -> resolve_inputs: "No files match"

        class _State:
            async def get_data(self):
                return {"tool": "combine"}

            async def clear(self):
                return None

        class _Msg:
            text = missing
            chat = ns(id=1)
            bot = _Bot()

            def __init__(self):
                self.replies = []

            async def answer(self, text, **kwargs):
                self.replies.append(text)

        m = _Msg()
        manager = JobManager()
        asyncio.run(scraping.analysis_file_text(m, _State(), manager))  # must not raise SystemExit

        assert any("Ошибка" in r and "No files match" in r for r in m.replies)
        assert manager.active is False  # slot released on the error path


# --- scraper helpers ---

class TestScraping:
    def test_parse_channels(self):
        from scraper.scrape import parse_channels
        assert parse_channels("a, b  c") == ["a", "b", "c"]

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



# --- change photo: a picture sent to the bot goes to every account ---

class TestChangePhotoFromChat:
    def test_photo_path_is_used_for_every_account(self, tmp_path, monkeypatch):
        from functions.change_profile_photo import ChangeProfilePhotoFunc

        monkeypatch.chdir(tmp_path)  # no assets/photos here: the folder must not be read
        storage = _Storage()
        storage.sessions = [_Session("A"), _Session("B")]
        fn = ChangeProfilePhotoFunc(storage, ns(delay=[1], profile_pause=[0]))
        used = []

        async def fake_set(session, photo_path, report):
            used.append((session.name, photo_path))

        fn.set_profile_photo = fake_set
        msgs, report = collect()
        asyncio.run(fn.run(report, photo_path="/x/avatar.jpg"))

        assert used == [("A", "/x/avatar.jpg"), ("B", "/x/avatar.jpg")]
        assert not any("No photos" in m for m in msgs)

    def _upload(self, tmp_path, monkeypatch, message):
        from bot.routers import profile

        monkeypatch.setattr(profile, "PHOTO_TMP_DIR", str(tmp_path / "photo"))

        class _State:
            cleared = False

            async def clear(self):
                _State.cleared = True

        class _Manager:
            path_during_job = None

            async def run(self, bot, chat_id, pool, instance, bot_function, factory, *a, **kw):
                func = ns(run=None)

                async def fake_run(reporter, photo_path=None):
                    _Manager.path_during_job = photo_path
                    assert os.path.exists(photo_path)

                func.run = fake_run
                await factory(func, None)
                return True

        state = _State()
        asyncio.run(profile.photo_upload(message, state, ns(), {"ChangeProfilePhotoFunc": object()}, _Manager()))
        return state, _Manager

    def _msg(self, photo=None, document=None):
        class _DlBot(_Bot):
            async def download(self, source, destination, timeout=None):
                with open(destination, "wb") as f:
                    f.write(b"img")

        msg = ns(photo=photo, document=document, chat=ns(id=1), bot=_DlBot(), replies=[])

        async def answer(text, **k):
            msg.replies.append(text)

        msg.answer = answer
        return msg

    def test_photo_is_downloaded_used_and_removed(self, tmp_path, monkeypatch):
        state, manager = self._upload(tmp_path, monkeypatch, self._msg(photo=[ns(), ns()]))

        assert state.cleared is True
        assert manager.path_during_job is not None
        assert not os.path.exists(manager.path_during_job)  # temp file cleaned after the job

    def test_image_document_is_accepted(self, tmp_path, monkeypatch):
        _, manager = self._upload(tmp_path, monkeypatch, self._msg(document=ns(mime_type="image/png")))
        assert manager.path_during_job is not None

    def test_non_image_keeps_waiting(self, tmp_path, monkeypatch):
        msg = self._msg(document=ns(mime_type="application/pdf"))
        state, manager = self._upload(tmp_path, monkeypatch, msg)

        assert state.cleared is False
        assert manager.path_during_job is None
        assert any("Ожидается картинка" in r for r in msg.replies)
