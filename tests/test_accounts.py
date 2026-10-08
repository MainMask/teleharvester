"""Offline tests for the bot accounts list (bot/routers/accounts.py)."""

import asyncio
import contextlib
import json
import types

from bot.routers import accounts
from modules.storages.sessions_storage import SessionsStorage


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

    def remember_username(self, client, username):
        pass

    def remember_name(self, client, first_name, last_name):
        pass

    fetch_me = SessionsStorage.fetch_me


class _Pool:
    def __init__(self, workers, storage):
        self._workers = workers
        self.storage = storage
        self.scraping = None

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

    asyncio.run(accounts.accounts(msg, pool, manager, None))

    joined = "\n".join(msg.replies)
    assert "всего: <b>2</b>" in joined
    assert "<b>1. Meggan Page</b>\n👤 @meg · 🆔 <code>111</code>" in joined
    assert "<b>2. Ivan</b>\n👤 — · 🆔 <code>222</code>" in joined
    assert "🆔 <code>111</code>\n❔ не проверялся\n\n<b>2." in joined  # blank line between cards
    assert manager.released is True


def test_sorted_by_username(tmp_path):
    def client(username, uid):
        return _Client(ns(first_name="Acc", last_name=None, id=uid, username=username))

    workers = [client("Curator2", 2), _Client(fail=True), client(None, 99), client("Curator10", 10),
               client("Curator1", 1), client("Curator4", 4)]
    pool = _Pool(workers, _Storage())
    msg = _Msg()

    asyncio.run(accounts.accounts(msg, pool, _Manager(free=True), None))

    joined = "\n".join(msg.replies)
    order = ["@Curator1 ", "@Curator2 ", "@Curator4 ", "@Curator10 ", "👤 — ", "не удалось опросить"]
    positions = [joined.index(marker) for marker in order]
    assert positions == sorted(positions)
    assert "<b>1. Acc</b>\n👤 @Curator1 " in joined
    assert "<b>6.</b> ⚠️ не удалось опросить" in joined


def test_busy_shows_only_count(tmp_path):
    pool = _Pool([_Client(ns(first_name="A", last_name=None, id=1, username=None))], _Storage())
    manager = _Manager(free=False)
    msg = _Msg()

    asyncio.run(accounts.accounts(msg, pool, manager, None))

    joined = "\n".join(msg.replies)
    assert "всего: <b>1</b>" in joined
    assert "Идёт задача" in joined
    assert "🆔" not in joined  # no per-account details while busy


def test_failed_worker_falls_back_to_stored(tmp_path):
    client = _Client(fail=True)
    client.path = "sessions/x.jsession"
    stored = ns(account=ns(account=ns(first_name="Stored", last_name="Name",
                                       phone_number="79990001122", user_id=333)))
    pool = _Pool([client], _Storage({"sessions/x.jsession": stored}))
    manager = _Manager(free=True)
    msg = _Msg()

    asyncio.run(accounts.accounts(msg, pool, manager, None))

    joined = "\n".join(msg.replies)
    assert "Stored Name" in joined
    assert "+79990001122" in joined
    assert "не удалось опросить" in joined
    assert manager.released is True


def test_scraping_worker_is_not_polled(tmp_path):
    # its auth key is in use by the scrape's own client: a second connection (another
    # proxy IP) risks AUTH_KEY_DUPLICATED
    polled = []

    class _Tracked(_Client):
        async def get_me(self):
            polled.append(self.path)
            return await super().get_me()

    free = _Tracked(ns(first_name="Free", last_name=None, id=1, username="free"))
    free.path = "sessions/free.jsession"
    busy = _Tracked(ns(first_name="Busy", last_name=None, id=2, username="busy"))
    busy.path = "sessions/busy.jsession"
    stored = ns(account=ns(account=ns(first_name="Busy", last_name=None,
                                       phone_number="79990002233", user_id=2)))
    pool = _Pool([free, busy], _Storage({"sessions/busy.jsession": stored}))
    pool.scraping = ns(path="sessions/busy.jsession")
    msg = _Msg()

    asyncio.run(accounts.accounts(msg, pool, _Manager(free=True), None))

    joined = "\n".join(msg.replies)
    assert polled == ["sessions/free.jsession"]
    assert "занят скрапом" in joined and "+79990002233" in joined
    assert "не удалось опросить" not in joined


def test_empty_pool(tmp_path):
    pool = _Pool([], _Storage())
    manager = _Manager(free=True)
    msg = _Msg()

    asyncio.run(accounts.accounts(msg, pool, manager, None))

    assert any("Воркеров: <b>0</b>" in r for r in msg.replies)


def test_names_are_html_escaped(tmp_path):
    workers = [_Client(ns(first_name="A&lt;B", last_name="<i>", id=1, username=None))]
    pool = _Pool(workers, _Storage())
    msg = _Msg()

    asyncio.run(accounts.accounts(msg, pool, _Manager(free=True), None))

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

    asyncio.run(asyncio.wait_for(accounts.accounts(msg, pool, manager, None), 2))

    assert "не удалось опросить" in "\n".join(msg.replies)
    assert manager.released is True



def test_polled_workers_are_busy_meanwhile(tmp_path):
    seen = []

    class _Seen(_Client):
        async def get_me(self):
            seen.append(list(pool.in_job))  # a scrape asking pool.busy() now must be refused
            return await super().get_me()

    workers = [_Seen(ns(first_name="A", last_name=None, id=1, username="a"))]
    pool = _Pool(workers, _Storage())
    pool.in_job = []
    asyncio.run(accounts.accounts(_Msg(), pool, _Manager(free=True), None))
    assert seen == [workers] and pool.in_job == []


def test_restrictions_are_shown(tmp_path):
    from modules import restricted_workers

    until = _Client(ns(first_name="Until", last_name=None, id=1, username="until"))
    until.path = "sessions/until.jsession"
    forever = _Client(fail=True)  # a stored card gets the mark too
    forever.path = "sessions/forever.jsession"
    stored = ns(account=ns(account=ns(first_name="Forever", last_name=None,
                                       phone_number="79990003344", user_id=2)))
    clean = _Client(ns(first_name="Clean", last_name=None, id=3, username="clean"))
    clean.path = "sessions/clean.jsession"
    new = _Client(ns(first_name="New", last_name=None, id=4, username="new"))
    new.path = "sessions/new.jsession"
    restricted_workers.save_status({until.path: "12 Nov 2026", clean.path: "active"})
    restricted_workers.save([forever.path])
    pool = _Pool([until, forever, clean, new], _Storage({forever.path: stored}))
    msg = _Msg()

    asyncio.run(accounts.accounts(msg, pool, _Manager(free=True), None))

    joined = "\n".join(msg.replies)
    assert "👤 @until · 🆔 <code>1</code>\n🚫 ЛС ограничены до 12 Nov 2026" in joined
    assert "не удалось опросить\n⛔ ограничен бессрочно" in joined
    assert "👤 @clean · 🆔 <code>3</code>\n✅ без ограничений" in joined
    assert "👤 @new · 🆔 <code>4</code>\n❔ не проверялся" in joined  # never checked


def test_restricted_go_to_the_bottom(tmp_path):
    from modules import restricted_workers

    def client(name, uid):
        c = _Client(ns(first_name=name, last_name=None, id=uid, username=name))
        c.path = f"sessions/{name}.jsession"
        return c

    # by username alone they would stay a, b, c
    forever, until, clean = client("a", 1), client("b", 2), client("c", 3)
    restricted_workers.save([forever.path])
    restricted_workers.save_status({until.path: "12 Nov 2026", clean.path: "active"})
    pool = _Pool([forever, until, clean], _Storage())
    msg = _Msg()

    asyncio.run(accounts.accounts(msg, pool, _Manager(free=True), None))

    joined = "\n".join(msg.replies)
    assert "всего: <b>3</b> · ограничены: <b>2</b>" in joined
    order = ["<b>1. c</b>", "<b>2. b</b>", "<b>3. a</b>"]
    positions = [joined.index(marker) for marker in order]
    assert positions == sorted(positions)


# --- personal accounts: listed from their files, removable ------------------------------------

class _Personal:
    """A personal_sessions/ storage of one account."""

    def __init__(self, path, client):
        self.full_sessions = {path: client}
        self.jsessions_paths = {}

    @property
    def sessions(self):
        return list(self.full_sessions.values())

    def get_session_path(self, client):
        return next(p for p, c in self.full_sessions.items() if c is client)

    def forget_session(self, path):
        self.full_sessions.pop(path, None)


class _Me:
    """A personal account's client: it must not be connected just to be listed."""

    def __init__(self, log_out_ok=True):
        self.connected = False
        self.logged_out = False
        self.log_out_ok = log_out_ok

    async def connect(self):
        self.connected = True

    def is_connected(self):
        return self.connected

    async def disconnect(self):
        self.connected = False

    async def log_out(self):
        if not self.log_out_ok:
            raise ConnectionError("dead network")
        self.logged_out, self.connected = True, False
        return True


class _State:
    def __init__(self):
        self.data = {}

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kw):
        self.data.update(kw)

    async def clear(self):
        self.data = {}


class _CbMsg(_Msg):
    async def answer(self, text, **kwargs):
        self.replies.append((text, kwargs.get("reply_markup")))


def _personal(tmp_path, log_out_ok=True):
    (tmp_path / "personal_sessions").mkdir()
    path = "personal_sessions/me.jsession"
    (tmp_path / path).write_text("{}")
    me = _Me(log_out_ok)
    return _Personal(path, me), me, path


def _remove(pool, personal, state=None):
    """🗑 Убрать → (one account: straight to the confirmation) → 🗑 Да; the replies."""
    from bot.callbacks import ChoiceCB

    state, msg = state or _State(), _CbMsg()
    callback = ns(message=msg, answer=lambda *a, **k: asyncio.sleep(0))
    asyncio.run(accounts.personal_remove_start(callback, state, personal))
    asyncio.run(accounts.personal_remove(callback, ChoiceCB(scope="prm_ok", value="0"), state, pool, personal))
    return [text for text, _ in msg.replies]


def _free_pool():
    pool = _Pool([], _Storage())
    pool.busy = lambda path: False
    return pool


def test_personal_accounts_are_listed_without_connecting(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    personal, me, _ = _personal(tmp_path)
    msg = _CbMsg()
    asyncio.run(accounts.accounts(msg, _Pool([], _Storage()), _Manager(free=True), personal))

    texts = [text for text, _ in msg.replies]
    assert any("Воркеров: <b>0</b>" in t for t in texts)  # shown with no worker too
    assert "👤 <b>Личные</b>" in texts[-1] and "me.jsession" in texts[-1] and not me.connected
    assert msg.replies[-1][1].inline_keyboard[0][0].text == "🗑 Убрать личный аккаунт"


def test_removal_logs_out_and_deletes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    personal, me, path = _personal(tmp_path)
    texts = _remove(_free_pool(), personal)
    assert "авторизацию из этого файла" in texts[0] and "Desktop тоже выйдет" in texts[0] and "✅" in texts[-1]
    assert me.logged_out and not (tmp_path / path).exists() and personal.sessions == []


def test_failed_log_out_still_deletes_and_says_where_to_end_it(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    personal, me, path = _personal(tmp_path, log_out_ok=False)
    texts = _remove(_free_pool(), personal)
    assert "Устройства" in texts[-1] and not (tmp_path / path).exists() and not me.connected


def test_no_removal_while_it_scrapes_or_has_an_unfinished_scrape(tmp_path, monkeypatch):
    from modules import json_file, scraped_files

    monkeypatch.chdir(tmp_path)
    personal, me, path = _personal(tmp_path)
    pool = _free_pool()
    pool.scraping = ns(path=path)
    assert "идёт скрап" in _remove(pool, personal)[-1]

    pool.scraping = None
    json_file.save(scraped_files.SCRAPE_MARKER, {"account": path})
    assert "незавершённый скрап" in _remove(pool, personal)[-1]
    assert not me.logged_out and (tmp_path / path).exists() and personal.sessions == [me]


def test_no_removal_with_a_checkpoint_left_by_a_stop(tmp_path, monkeypatch):
    """⏹ or an error drops the bot's marker but keeps the checkpoint: it continues on this account only."""
    monkeypatch.chdir(tmp_path)
    personal, me, path = _personal(tmp_path)
    resume = tmp_path / "assets" / "databases" / "news_partial" / "checkpoint" / "resume.json"
    resume.parent.mkdir(parents=True)
    resume.write_text(json.dumps({"name": "news", "account": path}))
    assert "незавершённый скрап" in _remove(_free_pool(), personal)[-1]
    assert not me.logged_out and (tmp_path / path).exists()
