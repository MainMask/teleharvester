"""The 📊 Progress button: Progress rendering, the function hooks that step it, the handler."""

import asyncio
import contextlib
import types
from unittest.mock import patch

from bot.routers.menu import show_progress
from bot.services import progress as progress_mod
from bot.services.jobs import JobManager
from bot.services.progress import Progress
from functions.base.telethon import TelethonFunction


def _storage(sessions=()):
    @contextlib.asynccontextmanager
    async def ainitialize_session(session):
        yield session

    return types.SimpleNamespace(sessions=list(sessions), ainitialize_session=ainitialize_session)


def _fn(sessions):
    return TelethonFunction(_storage(sessions), types.SimpleNamespace(delay=[0]))


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class TestRender:
    def test_counts_bar_and_eta_from_rate(self):
        clock = _Clock()
        with patch.object(progress_mod.time, "monotonic", clock):
            p = Progress()
            p.start(10)
            for _ in range(4):
                p.step()
            clock.now += 40  # 10 s per item -> 6 left = 60 s
            text = p.render("Рассылка в ЛС (10)…")

        assert "4 / 10" in text and "40%" in text
        assert "██████░░░░░░░░░" in text  # 6 of 15 cells
        assert "ETA 00:00:01:00" in text and "⏱ 00:00:00:40" in text

    def test_eta_estimating_before_first_step(self):
        p = Progress()
        p.start(5)
        assert "0 / 5" in p.render("job") and "ETA оценивается" in p.render("job")

    def test_without_total(self):
        assert "Прогресс недоступен" in Progress().render("Рассылка в чаты…")

    def test_fraction_and_eta_set_directly(self):
        p = Progress()
        p.set(0.5, 125)
        text = p.render("Скрап")
        assert "50%" in text and "ETA 00:00:02:05" in text and " / " not in text

    def test_fits_an_alert(self):
        p = Progress()
        p.start(1_000_000)
        p.step()
        assert len(p.render("x" * 500)) <= 200  # Telegram's show_alert text limit


class _Rec:
    def __init__(self):
        self.total, self.steps = None, 0

    def start(self, total):
        self.total = total

    def step(self):
        self.steps += 1


class TestHooks:
    def test_rotation_steps_per_processed_item(self):
        fn = _fn(["a"])
        fn.progress = _Rec()

        async def action(session, item):
            return None

        asyncio.run(fn.run_with_rotation([1, 2, 3], action))
        assert fn.progress.steps == 3

    def test_gather_in_order_counts_workers(self):
        fn = _fn(["a", "b"])
        fn.progress = _Rec()

        async def work(session, report):
            return session

        async def report(text):
            pass

        asyncio.run(fn.gather_in_order(work, report))
        assert (fn.progress.total, fn.progress.steps) == (2, 2)

    def test_no_progress_is_a_no_op(self):  # the CLI path
        fn = _fn(["a"])

        async def action(session, item):
            return None

        assert asyncio.run(fn.run_with_rotation([1], action)) == 1


class _Callback:
    def __init__(self):
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs.get("show_alert", False)))


def _slot(value):
    from bot.callbacks import ChoiceCB
    return ChoiceCB(scope="job_progress", value=value)


class TestHandler:
    def test_shows_alert_for_the_active_job(self):
        manager = JobManager()
        manager._label = "Рассылка в ЛС (3)…"
        manager.progress = Progress()
        manager.progress.start(3)
        callback = _Callback()

        asyncio.run(show_progress(callback, _slot("show"), manager, JobManager()))

        text, alert = callback.answers[0]
        assert alert and "0 / 3" in text and text.startswith("Рассылка в ЛС")

    def test_no_active_job(self):
        callback = _Callback()
        asyncio.run(show_progress(callback, _slot("show"), JobManager(), JobManager()))
        assert callback.answers == [("Нет активной задачи.", False)]

    def test_the_scraper_button_reads_the_scraper_slot(self):
        manager, scrapes = JobManager(), JobManager()
        scrapes._label = "Скрап"
        scrapes.progress = Progress()
        scrapes.progress.set(0.5, None, "Постов: 7")
        callback = _Callback()

        asyncio.run(show_progress(callback, _slot("scrape"), manager, scrapes))
        text, alert = callback.answers[0]
        assert alert and text.startswith("Скрап") and "Постов: 7" in text

    def test_job_run_exposes_progress_to_the_function_then_clears_it(self):
        seen = []

        class _Bot:
            async def send_message(self, *a, **k):
                return types.SimpleNamespace(message_id=1)

            async def edit_message_text(self, *a, **k):
                pass

        class _Pool:
            workers = []

            async def run(self, instance, bot_function, factory, report):
                instance.sessions = self.workers  # as WorkerPool.delegate
                await factory(instance)

        async def job(f, r):
            seen.append((f.progress, manager.progress))

        async def scenario():
            await manager.run(_Bot(), 1, _Pool(), instance, None, job, "H", "done")
            await asyncio.sleep(0.05)

        manager = JobManager()
        instance = types.SimpleNamespace()
        asyncio.run(scenario())

        fn_progress, manager_progress = seen[0]
        assert isinstance(fn_progress, Progress) and fn_progress is manager_progress
        assert instance.progress is None and manager.progress is None


class TestCounterAndDrop:
    def test_counter_mode_shows_count_and_rate(self):
        clock = _Clock()
        with patch.object(progress_mod.time, "monotonic", clock):
            p = Progress()
            p.start(None)
            assert "Отправлено: 0\n" in p.render("job")
            for _ in range(6):
                p.step()
            clock.now += 120
            text = p.render("Рассылка в комментарии…")

        assert "Отправлено: 6 · ~3.0/мин" in text and "⏱ 00:00:02:00" in text
        assert "ETA" not in text and "█" not in text

    def test_drop_shrinks_total_but_not_below_done(self):
        p = Progress()
        p.start(10)
        p.step()
        p.step()
        p.drop(3)
        assert p.total == 7
        p.drop(100)
        assert p.total == 2  # never below what was already done

    def test_drop_is_a_no_op_without_total(self):
        p = Progress()
        p.start(None)
        p.drop(5)
        assert p.total is None and p.counting


def _comments(sessions, messages_count, fail_on):
    """CommentsBroadcastFunc over fake sessions; fail_on: {name: send number raising AccountLimited}."""
    from functions.base.base import AccountLimited
    from functions.broadcast_comments import CommentsBroadcastFunc

    fn = CommentsBroadcastFunc(_storage(sessions), types.SimpleNamespace(delay=[0], messages_count=messages_count))
    fn.progress = Progress()
    sent = {}

    async def send(session, *args, **kwargs):
        sent[session.name] = sent.get(session.name, 0) + 1
        if sent[session.name] == fail_on.get(session.name):
            raise AccountLimited("limit")

    async def report(text):
        pass

    async def no_delay():
        pass

    fn.delay = no_delay
    with patch("functions.broadcast_comments.rich_message.send", send):
        asyncio.run(fn.run("https://t.me/chan/5", None, [0], report))
    return fn.progress


class _Acc:
    def __init__(self, name):
        self.name = name

    async def get_me(self):
        return types.SimpleNamespace(first_name=self.name)


class TestBroadcastProgress:
    def test_comments_limit_drops_the_unsent_rest(self):
        # 2 workers × 2 = 4; "b" is limited on its 2nd send -> its 1 unsent leaves the total
        p = _comments([_Acc("a"), _Acc("b")], 2, {"b": 2})
        assert (p.total, p.done) == (3, 3)
        assert "3 / 3" in p.render("x") and "100%" in p.render("x")

    def test_comments_unlimited_counts_only(self):
        p = _comments([_Acc("a")], 0, {"a": 4})
        assert p.counting and p.total is None and p.done == 3

    def test_broadcast_limit_drops_rest(self):
        from functions.base.base import AccountLimited
        from functions.broadcast import Broadcast
        from modules.rich_message import RichContent

        fn = Broadcast(_storage(), types.SimpleNamespace(delay=[0], messages_count=5, messages=["hi"]))
        fn.configure(choice=0, content=RichContent(text="hi"))
        fn.progress = Progress()
        fn.progress.start(5)
        outcomes = iter([None, None, AccountLimited("limit")])

        async def _send(*args, **kwargs):
            err = next(outcomes)
            if err is not None:
                raise err

        async def no_delay():
            pass

        async def report(text):
            pass

        fn._send, fn.delay = _send, no_delay
        asyncio.run(fn.broadcast(_Acc("a"), "chat", report))
        assert (fn.progress.total, fn.progress.done) == (2, 2)

    def test_instant_hands_its_progress_to_the_broadcast(self):
        from functions.broadcast import Broadcast
        from functions.broadcast_instant import InstantBroadcastFunc

        fn = InstantBroadcastFunc(_storage(["a", "b"]),
                                  types.SimpleNamespace(delay=[0], messages_count=3, messages=["hi"]))
        fn.progress = Progress()
        seen = []

        async def fake_broadcast(self, session, link, report):
            seen.append(self.progress)

        with patch.object(Broadcast, "broadcast", fake_broadcast):
            asyncio.run(fn.run(0, False, None, None, None, "chat", None))

        assert seen == [fn.progress, fn.progress] and fn.progress.total == 6

    def test_chat_listener_counts_only(self):
        from functions.broadcast_chat import BroadcastChatFunc

        fn = BroadcastChatFunc(_storage(), types.SimpleNamespace(delay=[0], messages_count=3, messages=["hi"]))
        fn.progress = Progress()
        broadcast = fn.prepare(0, False, None, None, None)
        assert broadcast.progress is fn.progress and fn.progress.counting


class TestPrepareNoteUpdate:
    def test_prepare_says_preparing_until_counting_starts(self):
        p = Progress()
        assert "Прогресс недоступен" in p.render("job")
        p.prepare()
        assert "Подготовка…" in p.render("job")
        p.start(4)
        assert "0 / 4" in p.render("job")

    def test_zero_total_is_complete(self):
        p = Progress()
        p.prepare()
        p.start(0)
        assert "100%" in p.render("job")

    def test_set_note_is_shown(self):
        p = Progress()
        p.set(0.25, None, "Постов: 42")
        assert "Постов: 42" in p.render("Скрап") and "25%" in p.render("Скрап")

    def test_update_restarts_the_count_on_a_new_total(self):
        clock = _Clock()
        with patch.object(progress_mod.time, "monotonic", clock):
            p = Progress()
            clock.now += 50  # prep time doesn't count towards the rate
            p.update(0, 100)
            clock.now += 10
            p.update(20, 100)
            text = p.render("Верификация")
        assert "20 / 100" in text and "ETA 00:00:00:40" in text


class _InvSession:
    def __init__(self, limit_after):
        self.limit_after, self.calls = limit_after, 0

    async def get_me(self):
        return types.SimpleNamespace(first_name="w")

    async def get_entity(self, destination):
        return types.SimpleNamespace()

    async def iter_participants(self, source):
        for uid in (1, 2, 3, 4):
            yield types.SimpleNamespace(id=uid, bot=False, deleted=False, is_self=False)

    async def __call__(self, request):
        from functions.base.base import AccountLimited

        self.calls += 1
        if self.calls > self.limit_after:
            raise AccountLimited("PEER_FLOOD")
        return types.SimpleNamespace(missing_invitees=[])


class TestMoreJobs:
    def test_inviting_limit_drops_the_rest_of_the_chunk(self):
        from functions.inviting import InvitingFunc

        session = _InvSession(limit_after=1)
        fn = InvitingFunc(_storage([session]), types.SimpleNamespace(delay=[0], invite_per_account_daily=0))
        fn.progress = Progress()

        async def parse_targets(link, report):
            return [1, 2, 3, 4]

        async def resolve_source(session, link):
            return None

        async def no_delay():
            pass

        async def report(text):
            pass

        fn.parse_targets, fn.resolve_source, fn.delay = parse_targets, resolve_source, no_delay
        asyncio.run(fn.run("src", "dst", [0], report))
        assert (fn.progress.total, fn.progress.done) == (1, 1)  # 3 never tried after the limit

    def test_report_replay_steps_per_worker(self):
        from functions.report import ReportFunc

        class _Bad(_Acc):
            async def get_me(self):
                raise RuntimeError("dead")

        fn = ReportFunc(_storage(), types.SimpleNamespace(delay=[0]))
        fn.progress = Progress()
        fn.progress.start(3)

        async def replay(*args):
            pass

        async def report(text):
            pass

        fn.replay = replay
        asyncio.run(fn.replay_rest([_Acc("a"), _Bad("b"), _Acc("c")], "peer", [1], "", [], report))
        assert fn.progress.done == 3  # a failed get_me still counts as handled

    def test_report_finish_shows_the_button_and_counts_the_first_account(self):
        from bot.routers import moderation

        sent, seen = [], []

        class _Bot:
            async def send_message(self, chat_id, text, reply_markup=None, **k):
                sent.append(reply_markup)
                return types.SimpleNamespace(message_id=1)

            async def edit_message_text(self, *a, **k):
                pass

        class _Instance:
            progress = None

            async def replay_rest(self, rest, *args):
                seen.append((self.progress.total, self.progress.done))

        class _First:
            async def disconnect(self):
                pass

        manager = JobManager()
        manager.acquire("Репорт", timeout=0)
        instance = _Instance()
        flow = {"session": _First(), "rest": ["b", "c"], "peer": "p", "ids": [1], "comment": "",
                "selections": []}
        asyncio.run(moderation._finish(instance, flow, _Bot(), 1, manager, types.SimpleNamespace(in_job=[])))

        assert seen == [(3, 1)]
        assert sent[0].inline_keyboard[0][0].text == "📊 Прогресс"
        assert instance.progress is None and manager.progress is None and not manager.active

    def test_report_user_steps_per_worker(self):
        from functions.report_user import ReportUserFunc

        class _Rep(_Acc):
            async def __call__(self, request):
                pass

        fn = ReportUserFunc(_storage([_Rep("a"), _Rep("b")]), types.SimpleNamespace(delay=[0]))
        fn.progress = Progress()

        async def report(text):
            pass

        asyncio.run(fn.run("@x", None, "", report))
        assert (fn.progress.total, fn.progress.done) == (2, 2)


def test_drop_button_removes_the_markup_and_ignores_errors():
    from bot.routers.scraping import _drop_button

    edits = []

    class _Status:
        def __init__(self, fail):
            self.fail = fail

        async def edit_reply_markup(self, reply_markup=None):
            if self.fail:
                raise RuntimeError("message is not modified")
            edits.append(reply_markup)

    asyncio.run(_drop_button(_Status(fail=False)))
    asyncio.run(_drop_button(_Status(fail=True)))
    asyncio.run(_drop_button(None))  # the status message was never sent
    assert edits == [None]


class TestRecentRate:
    def test_eta_follows_the_recent_rate_not_the_average(self):
        clock = _Clock()
        with patch.object(progress_mod.time, "monotonic", clock):
            p = Progress()
            p.start(100)
            for _ in range(20):  # slow: 10 s per step
                clock.now += 10
                p.step()
            for _ in range(20):  # then fast: 1 s per step
                clock.now += 1
                p.step()
            text = p.render("job")
        # the window holds only the fast steps: 60 left × 1 s (the average would say 330 s)
        assert "40 / 100" in text and "ETA 00:00:01:00" in text

    def test_a_stall_raises_the_eta(self):
        clock = _Clock()
        with patch.object(progress_mod.time, "monotonic", clock):
            p = Progress()
            p.start(10)
            for _ in range(5):
                clock.now += 2
                p.step()
            before = p.render("job")
            clock.now += 90  # a flood wait: no steps
            after = p.render("job")
        assert "ETA 00:00:00:10" in before and "ETA 00:00:01:40" in after

    def test_counter_rate_is_recent(self):
        clock = _Clock()
        with patch.object(progress_mod.time, "monotonic", clock):
            p = Progress()
            p.start(None)
            for _ in range(20):
                clock.now += 60  # 1/мин
                p.step()
            for _ in range(20):
                clock.now += 10  # 6/мин
                p.step()
            assert "~6.0/мин" in p.render("job")

    def test_finishing_at_100_percent(self):
        p = Progress()
        p.start(2)
        p.step()
        p.step()
        text = p.render("job")
        assert "100%" in text and "Завершение…" in text and "ETA" not in text

        p = Progress()
        p.set(1.0, 0, "Постов: 9")  # the scraper writing its files
        assert "Завершение…" in p.render("Скрап")
