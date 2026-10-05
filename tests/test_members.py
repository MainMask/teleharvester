"""Offline tests for scraper.members: a group's member list into a participants base."""

import types

import pytest
from telethon.errors import ChatAdminRequiredError, FloodWaitError
from telethon.tl.types import InputPeerUser, User

import scraper.members as members
import scraper.scrape as scrape
from bot.services.delegation import WorkerPool
from modules import parquet_db, scraped_files
from scraper.config import Credentials
from scraper.members import MembersParams

_HASH = 2**60 + 3  # beyond float64's exact range


def _user(uid, **kw):
    return User(id=uid, access_hash=_HASH + uid, username=kw.get("username"),
                first_name=kw.get("first_name", f"U{uid}"), last_name=None,
                bot=kw.get("bot", False), deleted=kw.get("deleted", False))


class _Listing:
    """What iter_participants returns: an async iterator whose .total is set once iterated."""

    def __init__(self, users, total):
        self._users, self._total, self.total = users, total, None

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        self.total = self._total
        for user in self._users:
            yield user


class FakeClient:
    chats = {}       # chat arg -> (plain listing, total, {search char: users}) or an exception
    instances = []

    def __init__(self, *a, **k):
        type(self).instances.append(self)

    async def connect(self):
        pass

    async def is_user_authorized(self):
        return True

    async def get_me(self):
        return types.SimpleNamespace(id=4242)

    async def disconnect(self):
        pass

    async def get_input_entity(self, arg):
        return arg

    def iter_participants(self, entity, search=None):
        spec = self.chats[entity]
        if isinstance(spec, Exception):
            raise spec
        plain, total, by_char = spec
        return _Listing(plain if search is None else by_char.get(search, []), total)


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(scrape, "TelegramClient", FakeClient)
    FakeClient.instances = []
    return FakeClient


def _run(tmp_path, chats, **kw):
    return members.run(Credentials(1, "h", ""),
                       MembersParams(chats=chats, name="grp", out_dir=tmp_path, **kw))


def test_lists_members_skipping_bots_deleted_and_self(fake, tmp_path):
    fake.chats = {"@grp": ([_user(1, username="ann"), _user(2, bot=True), _user(3, deleted=True),
                            _user(4242), _user(5)], 5, {})}
    path, problems = _run(tmp_path, ["@grp"])

    rows = {r["user_id"]: r for r in parquet_db.load(str(path))}  # what the mailing reads
    assert set(rows) == {1, 5} and problems == []
    assert rows[1]["access_hash"] == _HASH + 1 and rows[1]["username"] == "ann"
    assert rows[1]["owner_id"] == 4242 and rows[1]["group"] == "@grp"
    assert path.name == "grp_participants_members.parquet"


def test_name_search_fills_in_past_the_listing_limit(fake, tmp_path):
    # the plain listing stops at 2 of 4; searching names reaches the rest (dupes ignored)
    fake.chats = {"@big": ([_user(1), _user(2)], 4,
                           {"a": [_user(2), _user(3)], "b": [_user(4)], "c": [_user(9)]})}
    calls = []
    path, problems = _run(tmp_path, ["@big"], on_progress=lambda d, t: calls.append((d, t)))

    assert {r["user_id"] for r in parquet_db.load(str(path))} == {1, 2, 3, 4}
    assert problems == []  # all 4 found: the search stopped before "c"
    assert calls[-1] == (4, 4)


def test_incomplete_listing_is_reported(fake, tmp_path):
    fake.chats = {"@big": ([_user(1)], 3, {})}
    _, problems = _run(tmp_path, ["@big"])
    assert problems == [("@big", "Telegram gave 1 of 3 members")]


def test_hidden_list_is_reported_and_the_next_chat_still_runs(fake, tmp_path):
    fake.chats = {"@chan": ChatAdminRequiredError(request=None), "@grp": ([_user(1)], 1, {})}
    path, problems = _run(tmp_path, ["@chan", "@grp"])

    assert {r["user_id"] for r in parquet_db.load(str(path))} == {1}
    assert problems[0][0] == "@chan" and "admins-only" in problems[0][1]


def test_flood_wait_keeps_what_was_listed(fake, tmp_path):
    fake.chats = {"@a": ([_user(1)], 1, {}), "@b": FloodWaitError(request=None, capture=900),
                  "@c": ([_user(3)], 1, {})}
    path, problems = _run(tmp_path, ["@a", "@b", "@c"])

    assert {r["user_id"] for r in parquet_db.load(str(path))} == {1}  # @c skipped after the wait
    assert problems == [("@b", "FLOOD_WAIT 900s - stopped, the rest skipped")]


def test_nobody_listed_writes_no_file(fake, tmp_path):
    fake.chats = {"@chan": ChatAdminRequiredError(request=None)}
    path, problems = _run(tmp_path, ["@chan"])
    assert path is None and len(problems) == 1 and not list(tmp_path.iterdir())


def test_the_base_is_offered_to_the_mailing(fake, tmp_path, monkeypatch):
    fake.chats = {"@grp": ([_user(1), _user(2)], 2, {})}
    path, _ = _run(tmp_path, ["@grp"])
    monkeypatch.setattr(scraped_files, "BASES_DIR", str(tmp_path))
    assert scraped_files.participant_bases() == [(str(path), "grp · members · 2")]


# --- the bot flow -------------------------------------------------------------------------

class _State:
    def __init__(self):
        self.data, self.state = {}, None

    async def set_state(self, state):
        self.state = state

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_data(self):
        return dict(self.data)

    async def clear(self):
        self.data, self.state = {}, None


class _Status:
    def __init__(self):
        self.markup_dropped = False

    async def edit_reply_markup(self, reply_markup=None):
        self.markup_dropped = reply_markup is None


class _Bot:
    def __init__(self):
        self.documents = []

    async def send_document(self, chat_id, document):
        self.documents.append(document.path)


class _Msg:
    def __init__(self, text, bot):
        self.text, self.bot, self.chat = text, bot, types.SimpleNamespace(id=1)
        self.answers, self.status = [], _Status()

    async def answer(self, text, reply_markup=None, **kw):
        self.answers.append((text, reply_markup))
        return self.status


def test_bot_flow_runs_with_progress_and_sends_the_base(monkeypatch, tmp_path):
    import asyncio

    from bot.routers import scraping as router
    from bot.services import scraping
    from bot.services.jobs import JobManager

    base = tmp_path / "grp_participants_members.parquet"
    base.write_bytes(b"x")
    manager, seen = JobManager(), []

    async def do_members(creds, params):
        params.on_progress(1, 2)
        seen.append((params.chats, params.name, manager.progress.render(manager.label)))
        return base, [("@chan", "the member list is hidden or admins-only")]

    monkeypatch.setattr(scraping, "do_members", do_members)
    monkeypatch.setattr(router, "build_credentials", lambda client: None)
    pool = WorkerPool(types.SimpleNamespace(  # one worker: the account
        sessions=[object()], jsessions_paths={}, get_session_path=lambda c: "sessions/w.jsession"))

    bot, state = _Bot(), _State()
    state.data["account"] = "sessions/w.jsession"
    asyncio.run(router.members_chats(_Msg("@grp, @chan", bot), state))
    msg = _Msg("grp", bot)
    asyncio.run(router.members_run(msg, state, pool, None, manager))  # manager: the scraper slot

    [(chats, name, popup)] = seen
    assert chats == ["@grp", "@chan"] and name == "grp" and "1 / 2" in popup
    button = msg.answers[0][1].inline_keyboard[0][0]
    assert button.text == "📊 Прогресс" and button.callback_data.endswith(":scrape")
    assert msg.status.markup_dropped and not manager.active and manager.progress is None
    assert bot.documents == [str(base)]
    assert any("@chan" in text for text, _ in msg.answers)  # the problem is shown


def test_bot_rejects_an_empty_group_list():
    import asyncio

    from bot.routers import scraping as router
    from bot.states import Members

    state = _State()
    state.state = Members.chats
    msg = _Msg(" , ", None)
    asyncio.run(router.members_chats(msg, state))
    assert state.state == Members.chats and "Не вижу ни одной группы" in msg.answers[0][0]


# --- ⏹: a stop keeps what is listed -----------------------------------------------------------

class _SlowListing(_Listing):
    """Lists one member, then a long wait: a stop must not wait for it."""

    async def _gen(self):
        import asyncio

        self.total = self._total
        yield self._users[0]
        await asyncio.sleep(3600)


def test_a_stop_keeps_the_members_listed_so_far(fake, tmp_path, monkeypatch):
    import threading

    slow = _SlowListing([_user(1)], 5)
    fake.chats = {"@slow": None, "@next": ([_user(2)], 1, {})}
    monkeypatch.setattr(FakeClient, "iter_participants",
                        lambda self, entity, search=None: slow if entity == "@slow" else
                        _Listing(*fake.chats[entity][:2]))
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()

    path, problems = _run(tmp_path, ["@slow", "@next"], stop=stop)  # ~1 s, not an hour
    assert {r["user_id"] for r in parquet_db.load(str(path))} == {1}  # @next not reached
    assert problems == [("@slow", "stopped - the members listed so far are saved")]


def test_bot_members_stop_says_the_base_is_saved(monkeypatch, tmp_path):
    import asyncio

    from bot.routers import scraping as router
    from bot.services import scraping
    from bot.services.jobs import JobManager

    base = tmp_path / "grp_participants_members.parquet"
    base.write_bytes(b"x")

    async def do_members(creds, params):
        await router.scrape_stop(types.SimpleNamespace(answer=_noop))  # the operator's ⏹
        assert params.stop.is_set()
        return base, [("@grp", "stopped - the members listed so far are saved")]

    monkeypatch.setattr(scraping, "do_members", do_members)
    monkeypatch.setattr(router, "build_credentials", lambda client: None)
    pool = WorkerPool(types.SimpleNamespace(
        sessions=[object()], jsessions_paths={}, get_session_path=lambda c: "sessions/w.jsession"))
    bot, state = _Bot(), _State()
    state.data.update(account="sessions/w.jsession", chats="@grp")
    msg = _Msg("grp", bot)
    asyncio.run(router.members_run(msg, state, pool, None, JobManager()))

    assert _buttons_of(msg.answers[0][1]) == ["📊 Прогресс", "⏹ Стоп"]
    assert bot.documents == [str(base)] and msg.answers[-1][0].startswith("⏹ Остановлено")
    assert router._job_stop is None


async def _noop(*a, **k):
    pass


def _buttons_of(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def test_bot_members_on_a_logged_out_account_reports_instead_of_exiting(monkeypatch):
    """connect() raises SystemExit on a dead session; it must not escape the handler
    (a SystemExit out of an aiogram task stops the whole bot)."""
    import asyncio

    from bot.routers import scraping as router
    from bot.services import scraping
    from bot.services.jobs import JobManager

    async def do_members(creds, params):
        raise SystemExit("the worker's session is no longer authorized - re-add the account")

    monkeypatch.setattr(scraping, "do_members", do_members)
    monkeypatch.setattr(router, "build_credentials", lambda client: None)
    pool = WorkerPool(types.SimpleNamespace(
        sessions=[object()], jsessions_paths={}, get_session_path=lambda c: "sessions/w.jsession"))
    bot, state, manager = _Bot(), _State(), JobManager()
    state.data.update(account="sessions/w.jsession", chats="@grp")
    msg = _Msg("grp", bot)
    asyncio.run(router.members_run(msg, state, pool, None, manager))

    assert "no longer authorized" in msg.answers[-1][0]
    assert not manager.active and router._job_stop is None


def test_bot_members_wont_run_on_a_worker_in_a_bot_job(monkeypatch):
    import asyncio

    from bot.routers import scraping as router
    from bot.services import scraping
    from bot.services.jobs import JobManager

    calls = []

    async def do_members(creds, params):
        calls.append(params)
        return None, []

    monkeypatch.setattr(scraping, "do_members", do_members)
    monkeypatch.setattr(router, "build_credentials", lambda client: None)
    pool = WorkerPool(types.SimpleNamespace(
        sessions=[object()], jsessions_paths={}, get_session_path=lambda c: "sessions/w.jsession"))
    pool.in_job = pool.workers  # a mailing runs on it
    bot, state, scrapes = _Bot(), _State(), JobManager()
    state.data.update(account="sessions/w.jsession", chats="@grp")
    msg = _Msg("grp", bot)
    asyncio.run(router.members_run(msg, state, pool, None, scrapes))

    assert calls == [] and msg.answers[-1][0] == router.WORKER_BUSY and not scrapes.active


def test_private_chat_has_no_member_list(fake, tmp_path, monkeypatch):
    class UserClient(FakeClient):
        async def get_input_entity(self, arg):
            return InputPeerUser(5, 1)

    monkeypatch.setattr(scrape, "TelegramClient", UserClient)
    path, problems = _run(tmp_path, ["@bob"])
    assert path is None and problems == [("@bob", "a private chat has no member list")]


def test_stop_works_while_connecting(fake, tmp_path, monkeypatch):
    import asyncio
    import threading

    class Hanging(FakeClient):
        async def connect(self):
            await asyncio.sleep(60)  # a dead proxy: Telethon retries for hours

    monkeypatch.setattr(scrape, "TelegramClient", Hanging)
    stop = threading.Event()
    threading.Timer(0.1, stop.set).start()
    path, problems = _run(tmp_path, ["@grp"], stop=stop)
    assert path is None and problems == [("@grp", "stopped before the listing started")]
