"""Regression tests for the bugs confirmed in the project-wide code review."""

import asyncio
import contextlib
import html
import types

import openpyxl
import pandas as pd
from git.exc import GitCommandError

from functions.base.base import BaseFunction


def ns(**kw):
    return types.SimpleNamespace(**kw)


def collect():
    msgs = []

    async def report(text):
        msgs.append(text)

    return msgs, report


class _Storage:
    initialize = False

    def __init__(self, sessions=()):
        self.sessions = list(sessions)
        self.forgotten = []

    @contextlib.asynccontextmanager
    async def ainitialize_session(self, session):
        yield

    def _forget_session(self, path):
        self.forgotten.append(path)


async def _no_sleep(*_):
    return None


# --- functions/ --------------------------------------------------------------

class TestJoinerCaptcha:
    def test_handler_only_no_run_until_disconnected(self):
        from functions.joiner import JoinerFunc

        class _S:
            handlers = []

            def add_event_handler(self, cb, ev):
                self.handlers.append(cb)

            async def run_until_disconnected(self):  # would disconnect on cancel
                raise AssertionError("must not be used")

        fn = JoinerFunc(_Storage(), ns(delay=[0]))
        s = _S()
        assert fn.solve_captcha(s) is None  # plain call, no task to cancel
        assert s.handlers == [fn.on_message]


class TestCtrlCCancelsFunction:
    def test_task_cancelled_and_sessions_reconnected(self):
        from modules.storages.functions_storage import FunctionsStorage

        loop = asyncio.new_event_loop()
        cancelled = []

        class _Client:
            connected = False

            def is_connected(self):
                return self.connected

            async def connect(self):
                self.connected = True

        client = _Client()

        async def listener():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

        def _interrupt():
            raise KeyboardInterrupt

        fs = FunctionsStorage.__new__(FunctionsStorage)
        fs.loop = loop
        fs.storage = ns(initialize=True, sessions=[client])
        fs.functions = [(ns(execute=listener), "doc")]

        loop.call_later(0.01, _interrupt)  # Ctrl-C while the loop idles, not inside the task
        try:
            fs.execute(0)
            assert False, "KeyboardInterrupt must propagate to the menu"
        except KeyboardInterrupt:
            pass
        finally:
            asyncio.set_event_loop(None)  # execute() set it; don't leak a closed loop
            loop.close()

        assert cancelled == [True]  # not left pending for the next menu function
        assert client.connected is True


class TestChangeNameBlankLines:
    def test_blank_lines_dropped(self, monkeypatch, tmp_path):
        from functions.changename import ChangeNameFunc

        (tmp_path / "assets").mkdir()
        (tmp_path / "assets" / "names.txt").write_text("Ivan\n\nPetr Petrov\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("functions.changename.console.input", lambda *_: "y")

        fn = ChangeNameFunc(_Storage(), ns(delay=[0]))
        fn.ask_accounts_count = lambda: None
        got = []

        async def run(report, names=None, **_):
            got.append(names)

        fn.run = run
        asyncio.run(fn.execute())

        assert got == [["Ivan", "Petr Petrov"]]


class TestSessionsStorage:
    def test_binary_session_file_is_skipped(self, tmp_path):
        from modules.storages.sessions_storage import SessionsStorage

        (tmp_path / "sqlite.session").write_bytes(b"SQLite format 3\x00\xff\xfe\x80")
        storage = SessionsStorage(str(tmp_path), 1, "hash", initialize=False)
        assert storage.sessions == []

    def test_authorization_check_error_skips_session(self, tmp_path):
        from modules.storages.sessions_storage import SessionsStorage

        storage = SessionsStorage(str(tmp_path), 1, "hash", initialize=False)

        class _S:
            async def connect(self):
                return None

            async def is_user_authorized(self):
                raise RuntimeError("network")

            async def disconnect(self):
                return None

        s = _S()
        storage.full_sessions["p"] = s
        asyncio.run(storage.check_session(s, "p"))  # must not raise into the init gather
        assert storage.sessions == []


class TestReportCli:
    def test_rest_goes_through_replay_rest(self, monkeypatch):
        from functions.report import ReportFunc

        class _S:
            async def get_me(self):
                return ns(first_name="A")

        first, other = _S(), _S()
        fn = ReportFunc(_Storage([first, other]), ns(delay=[0]))
        fn.ask_accounts_count = lambda: None
        answers = iter(["https://t.me/x", "1"])
        monkeypatch.setattr("functions.report.Prompt.ask", lambda *a, **k: next(answers))
        monkeypatch.setattr("functions.report.console.input", lambda *_: "c")

        async def resolve_and_report(*_):
            return [0]

        replayed = []

        async def replay_rest(sessions, *args):
            replayed.append(sessions)

        fn.resolve_and_report = resolve_and_report
        fn.replay_rest = replay_rest
        asyncio.run(fn.execute())

        assert replayed == [[other]]


class TestCommentsErrorReset:
    def test_success_resets_counter(self, monkeypatch):
        from functions.broadcast_comments import CommentsBroadcastFunc

        outcomes = iter([ValueError("x")] * 4 + [None] + [ValueError("x")] * 4 + [None])
        sends = []

        async def fake_send(*a, **k):
            sends.append(1)
            err = next(outcomes)
            if err is not None:
                raise err

        monkeypatch.setattr("functions.broadcast_comments.rich_message.send", fake_send)

        class _S:
            async def get_me(self):
                return ns(first_name="A")

        fn = CommentsBroadcastFunc(_Storage(), ns(delay=[0], messages_count=2))
        fn.delay = _no_sleep
        _, report = collect()
        asyncio.run(fn.broadcast(_S(), "chan", 1, None, report))

        assert len(sends) == 10  # 4 errors never reach the 5-error stop once reset


class TestSpamBlockMove:
    def test_moved_session_is_forgotten(self, monkeypatch, tmp_path):
        from functions.spamblock import SpamBlockFunc

        monkeypatch.chdir(tmp_path)
        (tmp_path / "sessions").mkdir()
        (tmp_path / "sessions" / "a.session").write_text("x")
        path = "sessions/a.session"

        storage = _Storage()
        storage.get_session_path = lambda s: path
        fn = SpamBlockFunc(storage, ns(delay=[0]))
        fn.move_restricted({"permanent": [object()]})

        assert (tmp_path / "sessions" / "restricted" / "permanent" / "a.session").exists()
        assert storage.forgotten == [path]

    def test_unblock_failure_is_reported(self):
        from telethon.errors import YouBlockedUserError
        from functions.spamblock import SpamBlockFunc

        class _S:
            def conversation(self, *_):
                raise YouBlockedUserError(request=None)

            async def __call__(self, request):
                raise RuntimeError("no unblock")

        fn = SpamBlockFunc(_Storage(), ns(delay=[0]))
        msgs, report = collect()
        assert asyncio.run(fn.check(_S(), report)) is None
        assert any("can't unblock" in m for m in msgs)


class TestPmMailingPause:
    def test_reversed_range(self, monkeypatch):
        from functions.pmmailing import PmMailingFunc

        monkeypatch.setattr("functions.pmmailing.asyncio.sleep", _no_sleep)
        fn = PmMailingFunc(_Storage(), ns(delay=[0], account_pause=[60, 30]))
        msgs, fn._report = collect()
        fn._active_accounts = {"first"}

        asyncio.run(fn.pause_between_accounts("second", "B"))  # randint(60, 30) used to raise

        assert any("pause" in m for m in msgs)


class TestClearChatsOffset:
    def test_repeats_until_offset_zero(self):
        from functions.clear_chats import ClearDialogsFunc

        offsets = iter([100, 0])
        calls = []

        class _S:
            async def iter_dialogs(self):
                yield ns(entity=ns(), id=1, title="chat")

            async def __call__(self, request):
                calls.append(request)
                return ns(offset=next(offsets))

        fn = ClearDialogsFunc(_Storage(), ns(delay=[0]))
        _, report = collect()
        asyncio.run(fn.clear(_S(), report))

        assert len(calls) == 2


class TestParseMessageLink:
    def test_query_string(self):
        assert BaseFunction.parse_message_link("https://t.me/durov/123?single") == ("durov", 123)

    def test_public_topic(self):
        assert BaseFunction.parse_message_link("https://t.me/durov/5/123") == ("durov", 123)

    def test_private_topic(self):
        peer, message_id = BaseFunction.parse_message_link("https://t.me/c/1234567890/5/89?comment=1")
        assert (peer.channel_id, message_id) == (1234567890, 89)


# --- modules/ ----------------------------------------------------------------

class TestSetupBroadcast:
    def test_requires_one_message(self, monkeypatch):
        from modules.settings import Settings

        answers = iter(["", "hi", "", "1-3", "go"])
        monkeypatch.setattr("modules.settings.console.input", lambda *_: next(answers))
        assert Settings.setup_broadcast() == (["hi"], [1, 3], "go")


class TestUpdaterPullFailure:
    def test_git_command_error_is_reported(self, monkeypatch):
        from modules import updater

        class _Remote:
            def pull(self):
                raise GitCommandError("pull", 1)

        monkeypatch.setattr(updater, "Repo",
                            lambda *_: ns(remote=lambda name: _Remote(), head=ns(commit=None)))
        printed = []
        console = ns(status=lambda *_: contextlib.nullcontext(), print=printed.append)

        updater.update(console)  # must not crash main before the menu

        assert any("Update failed" in p for p in printed)


# --- bot/ --------------------------------------------------------------------

class _Manager:
    def __init__(self):
        self.released = 0

    def release(self):
        self.released += 1


def _callback(value):
    answers = []

    async def answer(*a, **k):
        answers.append(a)

    cb = ns(message=ns(chat=ns(id=7), answer=answer), answer=answer, bot=None)
    return cb, ns(value=str(value)), answers


class TestReportFlowConcurrency:
    def teardown_method(self):
        from bot.routers import moderation
        moderation._FLOWS.clear()

    def test_double_tap_does_not_run_second_step(self):
        from bot.routers import moderation

        stepped = []

        async def step(flow, option):
            stepped.append(option)
            return "choose", []

        flow = {"busy": True, "selections": []}
        moderation._FLOWS[7] = (ns(step=step), flow, [ns(option=b"a")])
        cb, data, _ = _callback(0)
        asyncio.run(moderation.rm_choose(cb, data, _Manager()))

        assert stepped == [] and flow["selections"] == []

    def test_abort_during_step_does_not_release_foreign_slot(self):
        from bot.routers import moderation

        async def step(flow, option):
            moderation._FLOWS.pop(7, None)  # /cancel (_abort) ran while we awaited
            raise RuntimeError("disconnected")

        class _Session:
            async def disconnect(self):
                return None

        flow = {"busy": False, "selections": [], "session": _Session()}
        moderation._FLOWS[7] = (ns(step=step), flow, [ns(option=b"a")])
        cb, data, _ = _callback(0)
        manager = _Manager()
        asyncio.run(moderation.rm_choose(cb, data, manager))

        assert manager.released == 0
        assert 7 not in moderation._FLOWS

    def test_stale_index_ignored(self):
        from bot.routers import moderation

        flow = {"busy": False, "selections": []}
        moderation._FLOWS[7] = (ns(step=None), flow, [ns(option=b"a")])
        cb, data, _ = _callback(5)
        asyncio.run(moderation.rm_choose(cb, data, _Manager()))

        assert flow["selections"] == [] and flow["busy"] is False


class TestReporterLength:
    def test_text_capped_at_4096(self):
        from bot.services.runner import TelegramReporter

        sent = []

        class _Bot:
            async def edit_message_text(self, text, **k):
                sent.append(text)

        reporter = TelegramReporter(_Bot(), 1, header="H")
        reporter.message_id = 1
        reporter.lines = ["x" * 500] * 25
        asyncio.run(reporter.finish("Готово"))

        assert sent and len(sent[-1]) <= 4096
        assert sent[-1].startswith("Готово")


class TestMailLimit:
    def _run(self, text):
        from bot.routers import broadcasts

        replies, data, states = [], {}, []

        class _State:
            async def update_data(self, **kw):
                data.update(kw)

            async def set_state(self, s):
                states.append(s)

        async def answer(t, **k):
            replies.append(t)

        asyncio.run(broadcasts.mail_limit(ns(text=text, answer=answer), _State()))
        return replies, data, states

    def test_typo_reprompts(self):
        replies, data, states = self._run("1O0")
        assert data == {} and states == [] and "положительное" in replies[0]

    def test_dash_means_all(self):
        _, data, states = self._run("-")
        assert data == {"limit": None} and states

    def test_number(self):
        _, data, _ = self._run("100")
        assert data == {"limit": 100}


class TestReadPreview:
    def test_cut_never_splits_an_entity(self):
        from bot.routers.scraping import read_preview

        df = pd.DataFrame({"c": ["&" * 400] * 10})
        out = read_preview(df)
        body = out[len("<pre>"):out.index("</pre>")]
        assert html.escape(html.unescape(body)) == body  # well-formed escaping


# --- scraper/ ----------------------------------------------------------------

class TestExcelFormulas:
    def test_equals_prefixed_text_stays_text(self, tmp_path):
        from scraper.datafiles import save_table

        path = save_table(pd.DataFrame({"Content": ["=== NEWS ===", "ok"]}), tmp_path / "x", "excel")
        cell = openpyxl.load_workbook(path)["Sheet1"]["A2"]

        assert (cell.value, cell.data_type) == ("=== NEWS ===", "s")


class TestJoinReturnsTarget:
    def _join(self, link, mode, reply):
        from functions.joiner import JoinerFunc

        class _S:
            async def __call__(self, request):
                return reply(request)

        fn = JoinerFunc(_Storage(), ns(delay=[0]))
        _, report = collect()
        return asyncio.run(fn.join(_S(), link, 0, mode, report))

    def test_invite_link_returns_joined_chat(self):
        chat = ns(id=5)
        assert self._join("https://t.me/joinchat/abc", "1", lambda r: ns(chats=[chat])) is chat

    def test_falls_back_to_link_without_chats(self):
        assert self._join("@chan", "1", lambda r: ns(chats=[])) == "@chan"

    def test_mode2_returns_linked_chat(self):
        from telethon.tl.functions.channels import GetFullChannelRequest

        linked = ns(id=9)

        def reply(request):
            if isinstance(request, GetFullChannelRequest):
                return ns(full_chat=ns(linked_chat_id=9), chats=[ns(id=1), linked])
            return ns(chats=[])

        assert self._join("@channel", "2", reply) is linked


class TestAddContactsUsernameFallback:
    def _add(self, row):
        from telethon.tl.types import InputUser
        from functions.add_contacts import AddContactsFunc

        added_ids = []

        class _S:
            async def __call__(self, request):
                if isinstance(request.id, InputUser):  # foreign access_hash
                    raise ValueError("USER_ID_INVALID")
                added_ids.append(request.id)

            async def get_input_entity(self, username):
                return ns(username=username)

        fn = AddContactsFunc(_Storage(), ns(delay=[0]))
        msgs, fn._report = collect()
        fn.added = 0
        asyncio.run(fn.add_one(_S(), row))
        return fn.added, added_ids, msgs

    def test_falls_back_to_username(self):
        added, ids, _ = self._add({"user_id": 1, "access_hash": 2, "username": "bob"})
        assert added == 1 and ids[0].username == "bob"

    def test_no_username_is_skipped(self):
        added, _, msgs = self._add({"user_id": 1, "access_hash": 2})
        assert added == 0 and any("skip user_id=1" in m for m in msgs)


class TestProfilePhotoSkipsHiddenFiles:
    def test_only_regular_visible_files(self, monkeypatch, tmp_path):
        from functions.change_profile_photo import ChangeProfilePhotoFunc

        photos = tmp_path / "assets" / "photos"
        (photos / "sub").mkdir(parents=True)
        (photos / ".DS_Store").write_bytes(b"\x00")
        (photos / "a.jpg").write_bytes(b"jpg")
        monkeypatch.chdir(tmp_path)

        fn = ChangeProfilePhotoFunc(_Storage([object()] * 5), ns(delay=[0]))
        picked = []

        async def set_profile_photo(session, path, report):
            picked.append(path)

        fn.set_profile_photo = set_profile_photo
        _, report = collect()
        asyncio.run(fn.run(report))

        assert picked and all(p.endswith("a.jpg") for p in picked)


class TestScraperMenuChoice:
    def test_typo_reasks_instead_of_default(self):
        from scraper.menu import Prompt

        answers = iter(["xlsx", "2"])
        prompt = Prompt(input_fn=lambda _msg: next(answers))
        assert prompt.choice("Format", ["parquet", "excel"], "parquet") == "excel"


class TestCancelNonCancelable:
    def test_reports_job_keeps_running(self):
        from bot.routers import menu
        from bot.services.jobs import JobManager

        replies = []

        async def answer(text, **k):
            replies.append(text)

        class _State:
            async def get_state(self):
                return None

            async def clear(self):
                return None

        async def scenario():
            manager = JobManager()
            manager.acquire("Скрапинг", cancelable=False, timeout=0)
            await menu.cancel(ns(answer=answer), _State(), manager)
            return manager

        manager = asyncio.run(scenario())
        assert manager.active and "не прерывается" in replies[0]


class TestMailRunStats:
    def _run(self, active_after_load, load_stats):
        from bot.routers import broadcasts

        replies = []

        async def answer(text, **k):
            replies.append(text)

        class _State:
            async def get_data(self):
                return {"path": "x", "skip": True}

            async def clear(self):
                return None

        class _Manager:
            label = "Рассылка"
            checks = 0

            @property
            def active(self):
                type(self).checks += 1
                return active_after_load and type(self).checks > 1  # busy only after the await

        instance = ns(load_recipients=lambda p: ["a"], load_stats=load_stats,
                      filter_unsent=lambda r: r)
        functions = {"PmMailingFunc": instance}
        asyncio.run(broadcasts.mail_run(ns(answer=answer), _State(), None, None, functions,
                                        _Manager(), None))
        return replies

    def test_broken_stats_is_reported(self):
        def broken():
            raise ValueError("bad json")

        replies = self._run(False, broken)
        assert any("Статистика не прочитана" in r for r in replies)

    def test_job_started_meanwhile_keeps_its_stats(self):
        loaded = []
        replies = self._run(True, lambda: loaded.append(1))
        assert loaded == [] and any("Занят" in r for r in replies)


# --- round 3 -----------------------------------------------------------------

class TestCliLoopReuse:
    def test_functions_run_on_the_sessions_loop(self, tmp_path):
        from modules.storages.functions_storage import FunctionsStorage

        loop = asyncio.new_event_loop()
        try:
            fs = FunctionsStorage(str(tmp_path), ns(loop=loop, sessions=[]), ns())
            assert fs.loop is loop  # Telethon refuses calls from another loop
        finally:
            asyncio.set_event_loop(None)
            loop.close()


class TestStickerRequest:
    def test_hash_is_passed(self, monkeypatch):
        from functions.broadcast import Broadcast

        requests = []

        class _S:
            async def __call__(self, request):
                requests.append(request)
                return ns(documents=["doc"])

            async def send_file(self, *a, **k):
                return None

        fn = Broadcast(_Storage(), ns(delay=[0]))
        fn.configure(choice=4, sticker_set="https://t.me/addstickers/Pack", content=None)

        async def no_send(*a, **k):
            return None

        import functions.broadcast as mod
        monkeypatch.setattr(mod.rich_message, "send", no_send)
        asyncio.run(fn._send(_S(), "chat", None, None))

        assert requests[0].hash == 0 and requests[0].stickerset.short_name == "Pack"


class TestUserBannedInChannel:
    def test_is_account_limited_not_a_chat_leave(self):
        from telethon.errors import UserBannedInChannelError
        from functions.base.base import AccountLimited

        async def make():
            raise UserBannedInChannelError(request=None)

        fn = BaseFunction()
        try:
            asyncio.run(fn.safe_call(make))
            assert False, "expected AccountLimited"
        except AccountLimited:
            pass


class TestJobManagerStops:
    def test_double_stop_still_frees_slot(self, monkeypatch):
        from bot.services import jobs as jobs_mod
        from bot.services.jobs import JobManager

        class _Reporter:
            async def start(self):
                return None

            async def finish(self, text):
                await asyncio.sleep(0)

        class _Session:
            async def disconnect(self):
                await asyncio.sleep(0.01)

        monkeypatch.setattr(jobs_mod, "TelegramReporter", lambda *a, **k: _Reporter())

        async def scenario():
            manager = JobManager()

            async def factory(f, reporter):
                await asyncio.sleep(10)

            pool = ns(workers=[_Session()], run=lambda inst, bf, fac, rep: fac(None))
            await manager.run(None, 1, pool, None, None, factory, "job", "done")
            await asyncio.sleep(0)
            await manager.stop()
            await asyncio.sleep(0.005)  # inside _wrap's cleanup (disconnect)
            await manager.stop()        # second tap
            await asyncio.sleep(0.05)
            return manager

        manager = asyncio.run(scenario())
        assert manager.active is False

    def test_stop_while_starting_aborts_start(self, monkeypatch):
        from bot.services import jobs as jobs_mod
        from bot.services.jobs import JobManager

        manager = JobManager()

        class _Reporter:
            async def start(self):
                await manager.stop()  # /cancel lands while the status message is posted

            async def finish(self, text):
                return None

        monkeypatch.setattr(jobs_mod, "TelegramReporter", lambda *a, **k: _Reporter())
        started = asyncio.run(manager.run(None, 1, ns(workers=[]), None, None,
                                          lambda f, r: None, "job", "done"))

        assert started is False and manager.active is False


class TestReportCancelDuringConnect:
    def test_no_step_after_abort(self):
        from bot.routers import moderation

        stepped = []

        class _First:
            async def connect(self):
                moderation._FLOWS.pop(7, None)  # /cancel ran _abort while connecting

            async def disconnect(self):
                return None

        async def step(flow, option):
            stepped.append(option)
            return "done", None

        class _Manager:
            label = ""

            def acquire(self, *a, **k):
                return True

            def release(self):
                raise AssertionError("slot no longer ours")

        class _State:
            async def get_data(self):
                return {"link": "l", "ids": [1]}

            async def clear(self):
                return None

        async def answer(*a, **k):
            return None

        instance = ns(step=step)
        pool = ns(delegate=lambda inst: [_First()])
        functions = {"ReportFunc": instance}
        msg = ns(text="-", chat=ns(id=7), answer=answer, bot=None)
        try:
            asyncio.run(moderation.rm_begin(msg, _State(), pool, functions, _Manager()))
        finally:
            moderation._FLOWS.clear()

        assert stepped == []


class TestCancelInsideForm:
    def test_form_cancel_keeps_job(self):
        from bot.routers import menu

        stopped = []
        replies = []

        class _State:
            async def get_state(self):
                return "ChangeBio:text"

            async def clear(self):
                return None

        async def stop():
            stopped.append(1)
            return True

        async def answer(text, **k):
            replies.append(text)

        asyncio.run(menu.cancel(ns(answer=answer), _State(), ns(stop=stop, active=True, label="Рассылка")))
        assert stopped == [] and replies[0].startswith("Отменено.") and "/cancel ещё раз" in replies[0]


class TestReporterTrailingFlush:
    def test_burst_tail_is_shown(self):
        from bot.services.runner import TelegramReporter

        sent = []

        class _Bot:
            async def edit_message_text(self, text, **k):
                sent.append(text)

        async def scenario():
            r = TelegramReporter(_Bot(), 1, header="H", min_interval=0.05)
            r.message_id = 1
            await r("first")
            await r("second")  # inside min_interval
            await asyncio.sleep(0.1)

        asyncio.run(scenario())
        assert "second" in sent[-1]


class TestVideoSticker:
    def test_saved_as_webm(self):
        from bot.services.capture import _source_and_flags

        sticker = ns(is_video=True, is_animated=False)
        msg = ns(photo=None, video=None, animation=None, voice=None, video_note=None,
                 audio=None, sticker=sticker, document=None)
        assert _source_and_flags(msg)[1] == "sticker.webm"


class TestMailLimitSuperscript:
    def test_superscript_digit_reprompts(self):
        replies, data, _ = TestMailLimit()._run("²")
        assert data == {} and "положительное" in replies[0]


class TestNewFilesOverwritten:
    def test_rewritten_file_is_new(self, tmp_path):
        import os
        from bot.services.scraping import dir_snapshot, new_files

        f = tmp_path / "x_posts.parquet"
        f.write_text("old")
        before = dir_snapshot(str(tmp_path))
        os.utime(f, ns=(1, before["x_posts.parquet"] + 10**9))  # overwritten later
        assert new_files(str(tmp_path), before) == [str(f)]


class TestExcelHeaderFormula:
    def test_header_stays_text(self, tmp_path):
        from scraper.datafiles import save_table

        path = save_table(pd.DataFrame({"=promo": [1]}), tmp_path / "h", "excel")
        cell = openpyxl.load_workbook(path)["Sheet1"]["A1"]
        assert (cell.value, cell.data_type) == ("=promo", "s")


class TestScrapeFuncSwappedDates:
    def test_reports_and_does_not_run(self, monkeypatch):
        from functions import scraper as fs

        answers = iter(["@chan", "name", "out", "31.12.2024", "01.01.2024", "", "parquet"])
        monkeypatch.setattr(fs, "pick_session", lambda storage: object())
        monkeypatch.setattr(fs.Prompt, "ask", lambda *a, **k: next(answers))
        monkeypatch.setattr(fs.Confirm, "ask", lambda *a, **k: False)
        printed = []
        monkeypatch.setattr(fs.console, "print", lambda *a, **k: printed.append(str(a[0])))
        ran = []
        monkeypatch.setattr(fs, "scrape_run", lambda *a: ran.append(1))

        f = fs.ScrapeFunc(_Storage(), ns(delay=[0]))
        f.ask_int = lambda *a, **k: 100
        f.execute()

        assert ran == [] and any("is after" in p for p in printed)


class TestInvitingMissingInvitees:
    def test_privacy_restricted_not_counted(self):
        from functions.inviting import InvitingFunc

        users = [ns(id=1, bot=False, deleted=False, is_self=False),
                 ns(id=2, bot=False, deleted=False, is_self=False)]

        class _S:
            async def get_me(self):
                return ns(first_name="A")

            async def get_entity(self, dest):
                return dest

            async def iter_participants(self, source):
                for u in users:
                    yield u

            async def __call__(self, request):
                return ns(missing_invitees=[ns(user_id=1)] if request.users[0].id == 1 else [])

        fn = InvitingFunc(_Storage(), ns(delay=[0]))
        fn.delay = _no_sleep

        async def resolve_source(session, link):
            return "src"

        fn.resolve_source = resolve_source
        msgs, report = collect()
        asyncio.run(fn.invite(_S(), "src", "dest", [1, 2], report))

        invited = [m for m in msgs if "invited" in m]
        assert invited == ["[A] invited 2 total: 1"]


# --- round 4 -----------------------------------------------------------------

class TestGifSentAsAnimation:
    def test_capture_marks_animation(self):
        from bot.services.capture import _source_and_flags

        msg = ns(photo=None, video=None, animation=ns(file_name=None), voice=None,
                 video_note=None, audio=None, sticker=None, document=None)
        found = _source_and_flags(msg)
        assert found[1] == "animation.mp4" and found[-1] is True

    def test_send_passes_animated_attribute(self):
        from telethon import types as tl
        from modules.rich_message import MediaItem, RichContent, _send_once

        calls = []

        class _S:
            async def send_file(self, peer, path, **kw):
                calls.append(kw)

        async def safe_call(make):
            return await make()

        content = RichContent(text="", media=[MediaItem(path="a.mp4", animated=True)])
        asyncio.run(_send_once(_S(), "peer", content, [], safe_call))
        assert isinstance(calls[0]["attributes"][0], tl.DocumentAttributeAnimated)


class TestSessionFiles:
    def test_ipv6_string_session_loaded_garbage_skipped(self, tmp_path):
        from telethon.sessions import StringSession
        from modules.storages.sessions_storage import SessionsStorage

        ss = StringSession()
        ss.set_dc(2, "2001:67c:4e8:f002::a", 443)
        ss._auth_key = __import__("telethon.crypto", fromlist=["AuthKey"]).AuthKey(b"\x01" * 256)
        v6 = ss.save()
        assert len(v6) == 369
        (tmp_path / "v6.session").write_text(v6)
        (tmp_path / "junk.session").write_text("x" * 100)

        storage = SessionsStorage(str(tmp_path), 1, "hash", initialize=False)
        assert list(storage.full_sessions) == [str(tmp_path / "v6.session")]

    def test_broken_jsession_skipped(self, tmp_path):
        from modules.storages.sessions_storage import SessionsStorage

        (tmp_path / "broken.jsession").write_text("{not json")
        storage = SessionsStorage(str(tmp_path), 1, "hash", initialize=False)
        assert storage.sessions == []


class TestUpdaterAheadOfOrigin:
    def test_no_update_when_origin_is_ancestor(self, monkeypatch):
        from modules import updater

        upcoming = ns(hexsha="origin", message="m")
        repo = ns(
            remotes=ns(origin=ns(refs=ns(master=ns(commit=upcoming)))),
            head=ns(commit=ns(hexsha="local")),
            is_ancestor=lambda a, b: a is upcoming,
        )
        monkeypatch.setattr(updater.git, "Repo", lambda *a: repo)
        monkeypatch.setattr(updater.git, "Remote", lambda r, name: ns(fetch=lambda: ["info"]))
        monkeypatch.setattr(updater, "get_current_commit", lambda: "local")

        assert updater.check_update() == {"has_update": False}


# --- round 5 -----------------------------------------------------------------

class TestInviteJoinResult:
    def test_inviting_reads_chat_from_wrapped_updates(self):
        from telethon.tl.functions.messages import ImportChatInviteRequest
        from functions.inviting import InvitingFunc

        chat = ns(id=3)

        class _S:
            async def __call__(self, request):
                assert isinstance(request, ImportChatInviteRequest)
                return ns(updates=ns(chats=[chat]))  # ChatInviteJoinResultOk

        fn = InvitingFunc(_Storage(), ns(delay=[0]))
        assert asyncio.run(fn.resolve_source(_S(), "https://t.me/+abcdef")) is chat

    def test_joiner_reads_chat_from_wrapped_updates(self):
        chat = ns(id=5)
        got = TestJoinReturnsTarget()._join("https://t.me/joinchat/abc", "1",
                                            lambda r: ns(updates=ns(chats=[chat])))
        assert got is chat


class TestMentionsAndCaptions:
    def test_mention_converted_to_input_entity(self):
        from telethon import types as tl
        from modules.rich_message import RichContent, send

        sent = []

        class _S:
            async def get_input_entity(self, user_id):
                if user_id == 2:
                    raise ValueError("unknown")
                return tl.InputUser(user_id, 99)

            async def send_message(self, peer, text, formatting_entities=None, **kw):
                sent.append(formatting_entities)

        async def safe_call(make):
            return await make()

        entities = [tl.MessageEntityMentionName(0, 2, user_id=1),
                    tl.MessageEntityMentionName(2, 2, user_id=2),
                    tl.MessageEntityBold(0, 4)]
        asyncio.run(send(_S(), "peer", RichContent(text="abcd", entities=entities), safe_call))

        kinds = [type(e).__name__ for e in sent[0]]
        assert kinds == ["InputMessageEntityMentionName", "MessageEntityBold"]  # unknown user dropped

    def test_caption_not_parsed_as_markdown(self):
        from modules.rich_message import MediaItem, RichContent, _send_once

        calls = []

        class _S:
            async def send_file(self, peer, path, **kw):
                calls.append(kw)

        async def safe_call(make):
            return await make()

        content = RichContent(text="use **kwargs", media=[MediaItem(path="a.jpg")])
        asyncio.run(_send_once(_S(), "peer", content, [], safe_call))
        assert calls[0]["parse_mode"] is None


class TestClearChatsChannelForbidden:
    def test_banned_channel_is_left(self):
        from telethon import types as tl
        from telethon.tl.functions.channels import LeaveChannelRequest
        from functions.clear_chats import ClearDialogsFunc

        calls = []
        forbidden = tl.ChannelForbidden(id=1, access_hash=2, title="x")

        class _S:
            async def iter_dialogs(self):
                yield ns(entity=forbidden, id=-1001, title="x")

            async def __call__(self, request):
                calls.append(request)

        fn = ClearDialogsFunc(_Storage(), ns(delay=[0]))
        _, report = collect()
        asyncio.run(fn.clear(_S(), report))
        assert isinstance(calls[0], LeaveChannelRequest)


class TestScraperCredentials:
    def test_account_proxy_and_app_carried_over(self):
        from modules.scraper_creds import build_credentials

        init = ns(device_model="Redmi Note 10", system_version="SDK 31", app_version="10.3",
                  lang_code="en", system_lang_code="en-US")
        client = ns(api_id=7, api_hash="app", session=ns(save=lambda: "KEY"),
                    _proxy=("socks5", "1.1.1.1", 1080), _init_request=init)
        creds = build_credentials(client)
        assert (creds.api_id, creds.api_hash, creds.session_string, creds.proxy) == \
            (7, "app", "KEY", ("socks5", "1.1.1.1", 1080))
        assert creds.device == {"device_model": "Redmi Note 10", "system_version": "SDK 31",
                                "app_version": "10.3", "lang_code": "en", "system_lang_code": "en-US"}


class TestResumeCommandNumericIds:
    def test_parses_back(self, tmp_path):
        import shlex
        from datetime import datetime, timezone

        from scraper.cli import build_parser
        from scraper.scrape import ScrapeParams, _resume_command

        params = ScrapeParams(channels=["-1001629147115", "-1001234567890"],
                              date_min=datetime(2024, 1, 1, tzinfo=timezone.utc),
                              date_max=datetime(2024, 2, 1, tzinfo=timezone.utc),
                              name="n", keyword="-30%", out_dir=tmp_path)
        argv = shlex.split(_resume_command(params))[1:]  # drop "scraper"
        args = build_parser().parse_args(argv)
        assert args.channels == "-1001629147115,-1001234567890" and args.keyword == "-30%"


class TestVerifyFuncSwappedDates:
    def test_reports_and_does_not_run(self, monkeypatch):
        from functions import scraper as fs

        answers = iter(["posts.parquet", "@chan", "31.12.2024", "01.01.2024", ""])
        monkeypatch.setattr(fs, "pick_session", lambda storage: object())
        monkeypatch.setattr(fs.Prompt, "ask", lambda *a, **k: next(answers))
        printed = []
        monkeypatch.setattr(fs.console, "print", lambda *a, **k: printed.append(str(a[0])))
        ran = []
        monkeypatch.setattr(fs, "verify_run", lambda *a: ran.append(1))

        f = fs.VerifyFunc(_Storage(), ns(delay=[0]))
        f.ask_int = lambda *a, **k: 0
        f.execute()

        assert ran == [] and any("is after" in p for p in printed)


class TestExcelErrorStrings:
    def test_error_code_text_stays_text(self, tmp_path):
        from scraper.datafiles import save_table

        path = save_table(pd.DataFrame({"Content": ["#N/A"]}), tmp_path / "e", "excel")
        cell = openpyxl.load_workbook(path)["Sheet1"]["A2"]
        assert (cell.value, cell.data_type) == ("#N/A", "s")


class TestJobSlotBackstop:
    def test_cancel_before_first_step_frees_slot(self, monkeypatch):
        from bot.services import jobs as jobs_mod
        from bot.services.jobs import JobManager

        class _Reporter:
            async def start(self):
                return None

            async def finish(self, text):
                return None

        async def scenario():
            manager = JobManager()
            await manager.run(None, 1, ns(workers=[]), None, None, lambda f, r: None, "job", "done")
            manager._task.cancel()  # before _wrap ever runs: its try/finally never starts
            await asyncio.sleep(0.01)
            return manager

        monkeypatch.setattr(jobs_mod, "TelegramReporter", lambda *a, **k: _Reporter())
        manager = asyncio.run(scenario())
        assert manager.active is False


class TestReportAbortDuringAnswer:
    def test_no_step(self):
        from bot.routers import moderation

        stepped = []

        async def step(flow, option):
            stepped.append(option)
            return "choose", []

        async def answer(*a, **k):
            moderation._FLOWS.pop(7, None)  # /cancel ran _abort meanwhile

        flow = {"busy": False, "selections": []}
        moderation._FLOWS[7] = (ns(step=step), flow, [ns(option=b"a")])
        cb = ns(message=ns(chat=ns(id=7), answer=answer), answer=answer, bot=None)
        try:
            asyncio.run(moderation.rm_choose(cb, ns(value="0"), _Manager()))
        finally:
            moderation._FLOWS.clear()
        assert stepped == []


class TestScrapeSnapshotBeforeSlot:
    def test_unreadable_out_dir_keeps_slot_free(self, monkeypatch):
        from bot.routers import scraping
        from bot.services import scraping as svc
        from bot.services.jobs import JobManager

        def boom(path):
            raise PermissionError("denied")

        monkeypatch.setattr(svc, "dir_snapshot", boom)

        class _State:
            async def get_data(self):
                return {"out_dir": "/root/x", "channels": "@a", "name": "n", "date_min": "01.01.2024"}

            async def clear(self):
                return None

        replies = []

        async def answer(text, **k):
            replies.append(text)

        manager = JobManager()
        msg = ns(text="02.01.2024", chat=ns(id=1), bot=None, answer=answer)
        asyncio.run(scraping.scrape_run(msg, _State(), ns(workers=[object()]), manager))

        assert manager.active is False and any("недоступна" in r for r in replies)


class TestCheckSessionDisconnects:
    def test_unauthorized_client_disconnected(self, tmp_path):
        from modules.storages.sessions_storage import SessionsStorage

        path = tmp_path / "a.session"
        path.write_text("x")
        storage = SessionsStorage(str(tmp_path), 1, "hash", initialize=False)

        class _S:
            disconnected = False

            async def connect(self):
                return None

            async def is_user_authorized(self):
                return False

            async def disconnect(self):
                self.disconnected = True

        s = _S()
        storage.full_sessions[str(path)] = s
        asyncio.run(storage.check_session(s, str(path)))
        assert s.disconnected and (tmp_path / "inactive" / "a.session").exists()


class TestUpdaterRequirements:
    def test_installs_when_requirements_changed(self, monkeypatch):
        from modules import updater

        new_head = object()
        old_head = ns(diff=lambda other: [ns(b_path="requirements.txt")] if other is new_head else [])
        repo = ns(head=ns(commit=old_head))

        def pull():
            repo.head.commit = new_head  # HEAD moves; FetchInfo would carry no old_commit

        repo.remote = lambda name: ns(pull=pull)
        monkeypatch.setattr(updater, "Repo", lambda *_: repo)
        installed, restarted = [], []
        monkeypatch.setattr(updater, "update_requirements", lambda console: installed.append(1))
        monkeypatch.setattr(updater, "restart_app", lambda: restarted.append(1))
        console = ns(status=lambda *_: contextlib.nullcontext(), print=lambda *a: None)

        updater.update(console)

        assert installed == [1] and restarted == [1]


# --- round 5 leftovers ---------------------------------------------------------

class TestReporterLimits:
    def _reporter(self, sent, fail_with=None):
        from bot.services.runner import TelegramReporter

        class _Bot:
            async def edit_message_text(self, text, **k):
                if fail_with is not None:
                    raise fail_with
                sent.append(text)

        r = TelegramReporter(_Bot(), 1, header="H")
        r.message_id = 1
        return r

    def test_utf16_and_huge_header_fit(self):
        sent = []
        r = self._reporter(sent)
        r.lines = ["😀" * 300] * 25  # 600 UTF-16 units per line
        asyncio.run(r.finish("⚠️ Ошибка: " + "x" * 5000))

        text = sent[-1]
        assert len(text.encode("utf-16-le")) // 2 <= 4096
        assert text.startswith("⚠️ Ошибка: xxx")

    def test_retry_after_pauses_progress_edits(self):
        from aiogram.exceptions import TelegramRetryAfter

        sent = []
        err = TelegramRetryAfter(method=None, message="flood", retry_after=30)
        r = self._reporter(sent, fail_with=err)

        async def scenario():
            await r("one")  # hits flood control
            return r._last

        import time
        last = asyncio.run(scenario())
        assert last > time.monotonic() + 20  # next progress edit only after the wait


class TestSendFilesErrors:
    def test_failed_upload_reported_and_rest_sent(self, tmp_path):
        from bot.routers.scraping import _send_files

        a, b = tmp_path / "a.parquet", tmp_path / "b.parquet"
        a.write_text("a")
        b.write_text("b")
        docs, msgs = [], []

        class _Bot:
            async def send_document(self, chat_id, doc):
                if doc.path.endswith("a.parquet"):
                    raise RuntimeError("timeout")
                docs.append(doc.path)

            async def send_message(self, chat_id, text):
                msgs.append(text)

        asyncio.run(_send_files(_Bot(), 1, [str(a), str(b)]))
        assert docs == [str(b)] and "Не удалось отправить a.parquet" in msgs[0]


class TestJobNotRunLabel:
    def test_skipped_job_is_not_done(self, monkeypatch):
        from bot.services import jobs as jobs_mod
        from bot.services.jobs import JobManager

        finished = []

        class _Reporter:
            async def start(self):
                return None

            async def finish(self, text):
                finished.append(text)

        async def pool_run(*a):
            return False  # RISKY job, no workers

        async def scenario():
            manager = JobManager()
            await manager.run(None, 1, ns(workers=[], run=pool_run), None, None,
                              lambda f, r: None, "job", "Готово ✅")
            await asyncio.sleep(0.01)

        monkeypatch.setattr(jobs_mod, "TelegramReporter", lambda *a, **k: _Reporter())
        asyncio.run(scenario())
        assert finished == ["⚠️ Не выполнено"]


class TestReadTableKeepsNaText:
    def test_xlsx_and_csv(self, tmp_path):
        from scraper.datafiles import read_table, save_table

        df = pd.DataFrame({"Name": ["NA", "Nan", None], "n": [1, 2, 3]})
        for fmt in ("excel", "csv"):
            out = read_table(save_table(df, tmp_path / f"t_{fmt}", fmt))
            assert list(out["Name"][:2]) == ["NA", "Nan"] and pd.isna(out["Name"][2])


# --- round 6 -----------------------------------------------------------------

class TestInvitingPublicSource:
    def test_join_result_unwrapped(self):
        from functions.inviting import InvitingFunc

        chat = ns(id=4)

        class _S:
            async def __call__(self, request):
                return ns(updates=ns(chats=[chat]))  # JoinChannel -> ChatInviteJoinResultOk

        fn = InvitingFunc(_Storage(), ns(delay=[0]))
        assert asyncio.run(fn.resolve_source(_S(), "@public_chat")) is chat

    def test_basic_group_destination_stops_once(self):
        from functions.inviting import InvitingFunc

        users = [ns(id=i, bot=False, deleted=False, is_self=False) for i in (1, 2, 3)]

        from telethon import types as tl

        calls = []

        class _S:
            async def get_me(self):
                return ns(first_name="A")

            async def get_entity(self, dest):
                return tl.Chat(id=1, title="basic", photo=None, participants_count=3, date=None, version=1)

            async def iter_participants(self, source):
                for u in users:
                    yield u

            async def __call__(self, request):
                calls.append(request)

        fn = InvitingFunc(_Storage(), ns(delay=[0]))
        fn.delay = _no_sleep

        async def resolve_source(session, link):
            return "src"

        fn.resolve_source = resolve_source
        msgs, report = collect()
        asyncio.run(fn.invite(_S(), "src", "dest", [1, 2, 3], report))
        assert msgs == ["[A] destination is not a supergroup/channel"] and calls == []


class TestBasicGroupAdmins:
    def test_admins_by_participant_type(self):
        from telethon import types as tl
        from functions.broadcast import Broadcast

        members = [ns(id=1, participant=tl.ChatParticipant(user_id=1, inviter_id=0, date=None)),
                   ns(id=2, participant=tl.ChatParticipantAdmin(user_id=2, inviter_id=0, date=None)),
                   ns(id=3, participant=tl.ChatParticipantCreator(user_id=3))]
        mentioned = []

        class _S:
            async def get_me(self):
                return ns(first_name="A")

            async def get_participants(self, peer, filter=None):
                return members  # a basic group: Telethon ignores the filter

        fn = Broadcast(_Storage(), ns(delay=[0], messages_count=1, messages=["hi"]))
        fn.configure(choice=0, mention_all=True, mention_mode="admins", content=ns(text="hi", entities=[], media=[]))
        fn.delay = _no_sleep

        async def send(session, peer, content, report, reply_to=None):
            mentioned.extend(e.user_id for e in content.entities)

        fn._send = send
        _, report = collect()
        asyncio.run(fn.broadcast(_S(), "chat", report))
        assert sorted(mentioned) == [2, 3]


class TestHeartReaction:
    def test_variation_selector_stripped(self):
        from functions.reactions import ReactionsFunc

        sent = []

        class _S:
            async def get_me(self):
                return ns(first_name="A")

            async def __call__(self, request):
                sent.append(request.reaction[0].emoticon)

        fn = ReactionsFunc(_Storage(), ns(delay=[0]))
        _, report = collect()
        asyncio.run(fn.set_reaction(_S(), "chan", 1, report, reaction="❤️"))
        assert sent == ["❤"]


class TestCaptchaHandler:
    def _run(self, buttons):
        from functions.joiner import JoinerFunc

        clicked = []

        async def click(data=None):
            clicked.append(data)

        async def get_buttons():
            return buttons

        msg = ns(mentioned=True, get_buttons=get_buttons, click=click)
        asyncio.run(JoinerFunc(_Storage(), ns(delay=[0])).on_message(msg))
        return clicked

    def test_url_or_reply_button_ignored(self):
        assert self._run([[ns(data=None)]]) == []  # MessageButton.data is None there
        assert self._run(None) == []

    def test_callback_button_clicked_with_raw_bytes(self):
        data = b"\xff\x01captcha"  # not UTF-8
        assert self._run([[ns(data=data)]]) == [data]

    def test_real_layer_button_data(self):
        from telethon.tl import types as tl
        from telethon.tl.custom.messagebutton import MessageButton

        # the installed layer: KeyboardButton(text, type=InlineButtonTypeCallback(data))
        button = tl.KeyboardButton("go", tl.InlineButtonTypeCallback(data=b"\x01x"))
        assert MessageButton(None, button, None, None, 1).data == b"\x01x"


class TestUpdaterPipFailure:
    def test_reported_without_restart(self, monkeypatch):
        import subprocess
        from modules import updater

        new_head = object()
        old_head = ns(diff=lambda other: [ns(b_path="requirements.txt")])
        repo = ns(head=ns(commit=old_head))
        repo.remote = lambda name: ns(pull=lambda: setattr(repo.head, "commit", new_head))
        monkeypatch.setattr(updater, "Repo", lambda *_: repo)

        def fail(console):
            raise subprocess.CalledProcessError(1, ["pip"])

        monkeypatch.setattr(updater, "update_requirements", fail)
        restarted, printed = [], []
        monkeypatch.setattr(updater, "restart_app", lambda: restarted.append(1))
        console = ns(status=lambda *_: contextlib.nullcontext(), print=lambda *a: printed.append(a[0]))

        updater.update(console)
        assert restarted == [] and any("Requirements install failed" in p for p in printed)


class TestReporterFloodRetries:
    def test_final_survives_two_retry_afters(self, monkeypatch):
        from aiogram.exceptions import TelegramRetryAfter
        from bot.services import runner as runner_mod
        from bot.services.runner import TelegramReporter

        monkeypatch.setattr(runner_mod.asyncio, "sleep", _no_sleep)
        failures = [TelegramRetryAfter(method=None, message="flood", retry_after=1)] * 2
        sent = []

        class _Bot:
            async def edit_message_text(self, text, **k):
                if failures:
                    raise failures.pop()
                sent.append(text)

        r = TelegramReporter(_Bot(), 1, header="H")
        r.message_id = 1
        asyncio.run(r.finish("Готово ✅"))
        assert sent and sent[-1].startswith("Готово ✅")

    def test_progress_retry_after_schedules_flush(self):
        from aiogram.exceptions import TelegramRetryAfter
        from bot.services.runner import TelegramReporter

        failures = [TelegramRetryAfter(method=None, message="flood", retry_after=0)]
        sent = []

        class _Bot:
            async def edit_message_text(self, text, **k):
                if failures:
                    raise failures.pop()
                sent.append(text)

        async def scenario():
            r = TelegramReporter(_Bot(), 1, header="H", min_interval=0.01)
            r.message_id = 1
            await r("tail line")  # hits flood control, held back
            await asyncio.sleep(0.1)

        asyncio.run(scenario())
        assert sent and "tail line" in sent[-1]


class TestReportAnswerFailure:
    def test_busy_not_stuck(self):
        from bot.routers import moderation

        stepped = []

        async def step(flow, option):
            stepped.append(option)
            return "choose", [ns(option=b"b", text="b")]

        async def failing_answer(*a, **k):
            raise RuntimeError("query is too old")

        async def answer(*a, **k):
            return None

        flow = {"busy": False, "selections": []}
        moderation._FLOWS[7] = (ns(step=step), flow, [ns(option=b"a")])
        cb = ns(message=ns(chat=ns(id=7), answer=answer), answer=failing_answer, bot=None)
        try:
            asyncio.run(moderation.rm_choose(cb, ns(value="0"), _Manager()))
            assert stepped == [b"a"] and moderation._FLOWS[7][1]["busy"] is False
        finally:
            moderation._FLOWS.clear()


class TestSnapshotDanglingSymlink:
    def test_skipped(self, tmp_path):
        import os
        from bot.services.scraping import dir_snapshot

        (tmp_path / "real.parquet").write_text("x")
        os.symlink(tmp_path / "gone.parquet", tmp_path / "latest.parquet")
        assert list(dir_snapshot(str(tmp_path))) == ["real.parquet"]


class TestReadPreviewUtf16:
    def test_emoji_preview_fits(self):
        from bot.routers.scraping import read_preview

        out = read_preview(pd.DataFrame({"c": ["😀" * 1000] * 10}))
        body = out[len("<pre>"):out.index("</pre>")]
        assert len(html.unescape(body).encode("utf-16-le")) // 2 <= 3501


class TestCaptureTimeout:
    def test_download_gets_long_timeout(self, tmp_path):
        from bot.services.capture import _download

        seen = {}

        class _Bot:
            async def download(self, source, destination=None, timeout=30):
                seen["timeout"] = timeout

        asyncio.run(_download(_Bot(), str(tmp_path), 0, ("src", "a.mp4", False, False, False, False)))
        assert seen["timeout"] >= 300


class TestStopWhileFinishing:
    def test_completed_job_not_relabelled(self, monkeypatch):
        from bot.services import jobs as jobs_mod
        from bot.services.jobs import JobManager

        finished = []
        manager = JobManager()

        class _Reporter:
            async def start(self):
                return None

            async def finish(self, text):
                # Stop pressed while the final status waits out flood control
                stopped.append(await manager.stop())
                await asyncio.sleep(0.01)
                finished.append(text)

        stopped = []

        async def pool_run(inst, bf, fac, rep):
            return True

        async def scenario():
            await manager.run(None, 1, ns(workers=[], run=pool_run), None, None,
                              lambda f, r: None, "job", "Готово ✅")
            await asyncio.sleep(0.05)

        monkeypatch.setattr(jobs_mod, "TelegramReporter", lambda *a, **k: _Reporter())
        asyncio.run(scenario())
        assert stopped == [False] and finished == ["Готово ✅"] and manager.active is False
