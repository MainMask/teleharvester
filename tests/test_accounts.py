"""Offline tests for the bot accounts list (bot/routers/accounts/)."""

import asyncio
import contextlib
import json
import types

from bot.routers import accounts
from modules.storages.sessions_storage import SessionsStorage
from modules.types.proxy import Proxy


def ns(**kw):
    return types.SimpleNamespace(**kw)


def _me(premium=False, phone=None, **kw):  # a live profile: Telegram's User always carries these
    return ns(premium=premium, phone=phone, **kw)


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
    def __init__(self, jsessions=None, sessions=()):
        self.jsessions_paths = jsessions or {}
        self.sessions = list(sessions)

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

    def scraped(self, path):
        return self.scraping is not None and self.scraping.path == path


class _Manager:
    project_id = 1

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
        _Client(_me(first_name="Meggan", last_name="Page", id=111, username="meg")),
        _Client(_me(first_name="Ivan", last_name=None, id=222, username=None)),
    ]
    pool = _Pool(workers, _Storage())
    manager = _Manager(free=True)
    msg = _Msg()

    asyncio.run(accounts.workers.accounts(msg, _State(), pool, manager, None))

    joined = "\n".join(msg.replies)
    assert "всего: <b>2</b>" in joined
    assert "<b>1. Meggan Page</b>\n👤 @meg · 🆔 <code>111</code>" in joined
    assert "<b>2. Ivan</b>\n👤 — · 🆔 <code>222</code>" in joined
    assert "🆔 <code>111</code>\n❔ не проверялся\n\n<b>2." in joined  # blank line between cards
    assert manager.released is True


def test_premium_workers_are_marked(tmp_path):
    workers = [
        _Client(_me(first_name="Prem", last_name=None, id=1, username="prem", premium=True)),
        _Client(_me(first_name="Plain", last_name=None, id=2, username="plain")),
    ]
    msg = _Msg()

    asyncio.run(accounts.workers.accounts(msg, _State(), _Pool(workers, _Storage()), _Manager(free=True), None))

    joined = "\n".join(msg.replies)
    assert "👤 @prem · 🆔 <code>1</code> · ⭐ Premium" in joined
    assert "👤 @plain · 🆔 <code>2</code>\n" in joined


def test_sorted_by_username(tmp_path):
    def client(username, uid):
        return _Client(_me(first_name="Acc", last_name=None, id=uid, username=username))

    workers = [client("Curator2", 2), _Client(fail=True), client(None, 99), client("Curator10", 10),
               client("Curator1", 1), client("Curator4", 4)]
    pool = _Pool(workers, _Storage())
    msg = _Msg()

    asyncio.run(accounts.workers.accounts(msg, _State(), pool, _Manager(free=True), None))

    joined = "\n".join(msg.replies)
    order = ["@Curator1 ", "@Curator2 ", "@Curator4 ", "@Curator10 ", "👤 — ", "не удалось опросить"]
    positions = [joined.index(marker) for marker in order]
    assert positions == sorted(positions)
    assert "<b>1. Acc</b>\n👤 @Curator1 " in joined
    assert "<b>6.</b> ⚠️ не удалось опросить" in joined


def test_busy_shows_only_count(tmp_path):
    pool = _Pool([_Client(_me(first_name="A", last_name=None, id=1, username=None))], _Storage())
    manager = _Manager(free=False)
    msg = _Msg()

    asyncio.run(accounts.workers.accounts(msg, _State(), pool, manager, None))

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

    asyncio.run(accounts.workers.accounts(msg, _State(), pool, manager, None))

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

    free = _Tracked(_me(first_name="Free", last_name=None, id=1, username="free"))
    free.path = "sessions/free.jsession"
    busy = _Tracked(_me(first_name="Busy", last_name=None, id=2, username="busy"))
    busy.path = "sessions/busy.jsession"
    stored = ns(account=ns(account=ns(first_name="Busy", last_name=None,
                                       phone_number="79990002233", user_id=2)))
    pool = _Pool([free, busy], _Storage({"sessions/busy.jsession": stored}))
    pool.scraping = ns(path="sessions/busy.jsession")
    msg = _Msg()

    asyncio.run(accounts.workers.accounts(msg, _State(), pool, _Manager(free=True), None))

    joined = "\n".join(msg.replies)
    assert polled == ["sessions/free.jsession"]
    assert "занят скрапом" in joined and "+79990002233" in joined
    assert "не удалось опросить" not in joined


def test_empty_pool(tmp_path):
    pool = _Pool([], _Storage())
    manager = _Manager(free=True)
    msg = _Msg()

    asyncio.run(accounts.workers.accounts(msg, _State(), pool, manager, None))

    assert any("Воркеров: <b>0</b>" in r for r in msg.replies)


def test_names_are_html_escaped(tmp_path):
    workers = [_Client(_me(first_name="A&lt;B", last_name="<i>", id=1, username=None))]
    pool = _Pool(workers, _Storage())
    msg = _Msg()

    asyncio.run(accounts.workers.accounts(msg, _State(), pool, _Manager(free=True), None))

    joined = "\n".join(msg.replies)
    assert "A&amp;lt;B &lt;i&gt;" in joined


def test_slow_connect_is_timed_out(monkeypatch, tmp_path):
    class _SlowStorage(_Storage):
        @contextlib.asynccontextmanager
        async def ainitialize_session(self, client):
            await asyncio.sleep(10)  # a dead proxy: connect() hangs
            yield

    monkeypatch.setattr(accounts.workers, "GET_ME_TIMEOUT", 0.05)
    pool = _Pool([_Client(_me(first_name="A", last_name=None, id=1, username=None))], _SlowStorage())
    manager = _Manager(free=True)
    msg = _Msg()

    asyncio.run(asyncio.wait_for(accounts.workers.accounts(msg, _State(), pool, manager, None), 2))

    assert "не удалось опросить" in "\n".join(msg.replies)
    assert manager.released is True



def test_polled_workers_are_busy_meanwhile(tmp_path):
    seen = []

    class _Seen(_Client):
        async def get_me(self):
            seen.append(list(pool.in_job))  # a scrape asking pool.busy() now must be refused
            return await super().get_me()

    workers = [_Seen(_me(first_name="A", last_name=None, id=1, username="a"))]
    pool = _Pool(workers, _Storage())
    pool.in_job = []
    asyncio.run(accounts.workers.accounts(_Msg(), _State(), pool, _Manager(free=True), None))
    assert seen == [workers] and pool.in_job == []


def test_restrictions_are_shown(tmp_path):
    from modules import restricted_workers

    until = _Client(_me(first_name="Until", last_name=None, id=1, username="until"))
    until.path = "sessions/until.jsession"
    forever = _Client(fail=True)  # a stored card gets the mark too
    forever.path = "sessions/forever.jsession"
    stored = ns(account=ns(account=ns(first_name="Forever", last_name=None,
                                       phone_number="79990003344", user_id=2)))
    clean = _Client(_me(first_name="Clean", last_name=None, id=3, username="clean"))
    clean.path = "sessions/clean.jsession"
    new = _Client(_me(first_name="New", last_name=None, id=4, username="new"))
    new.path = "sessions/new.jsession"
    restricted_workers.save_status({until.path: "12 Nov 2026", clean.path: "active"})
    restricted_workers.save([forever.path])
    pool = _Pool([until, forever, clean, new], _Storage({forever.path: stored}))
    msg = _Msg()

    asyncio.run(accounts.workers.accounts(msg, _State(), pool, _Manager(free=True), None))

    joined = "\n".join(msg.replies)
    assert "👤 @until · 🆔 <code>1</code>\n🚫 ЛС ограничены до 12 Nov 2026" in joined
    assert "не удалось опросить\n⛔ ограничен бессрочно" in joined
    assert "👤 @clean · 🆔 <code>3</code>\n✅ без ограничений" in joined
    assert "👤 @new · 🆔 <code>4</code>\n❔ не проверялся" in joined  # never checked


def test_restricted_go_to_the_bottom(tmp_path):
    from modules import restricted_workers

    def client(name, uid):
        c = _Client(_me(first_name=name, last_name=None, id=uid, username=name))
        c.path = f"sessions/{name}.jsession"
        return c

    # by username alone they would stay a, b, c
    forever, until, clean = client("a", 1), client("b", 2), client("c", 3)
    restricted_workers.save([forever.path])
    restricted_workers.save_status({until.path: "12 Nov 2026", clean.path: "active"})
    pool = _Pool([forever, until, clean], _Storage())
    msg = _Msg()

    asyncio.run(accounts.workers.accounts(msg, _State(), pool, _Manager(free=True), None))

    joined = "\n".join(msg.replies)
    assert "всего: <b>3</b> · ограничены: <b>2</b>" in joined
    order = ["<b>1. c</b>", "<b>2. b</b>", "<b>3. a</b>"]
    positions = [joined.index(marker) for marker in order]
    assert positions == sorted(positions)


# --- personal accounts: listed from their files; each account's card, a personal one removable --

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

    async def edit_text(self, text, **kwargs):  # in place: what is shown now, as a reply is
        self.replies.append((text, kwargs.get("reply_markup")))


def _personal(tmp_path, log_out_ok=True):
    (tmp_path / "personal_sessions").mkdir()
    path = "personal_sessions/me.jsession"
    (tmp_path / path).write_text("{}")
    me = _Me(log_out_ok)
    return _Personal(path, me), me, path


def _press(scope, value, state, pool, personal, msg):
    """A button of the list, a card or a confirmation (ChoiceCB scope, value); a pop-up it answers with is kept
    in msg.replies too (its markup "pop-up"): what the operator sees after it."""
    from bot.callbacks import ChoiceCB

    async def answer(text=None, show_alert=False):
        if show_alert:
            msg.replies.append((text, "pop-up"))

    callback = ns(message=msg, answer=answer)
    handler = {"acc": accounts.card.account_card, "acc_act": accounts.card.account_action,
               "prm_ok": accounts.card.personal_remove}
    asyncio.run(handler[scope](callback, ChoiceCB(scope=scope, value=value), state, pool, personal))


def _key(state, index):
    """A list's button key: the index and the list's token."""
    return f"{index}.{state.data['acc_token']}"


def _remove(pool, personal, state=None):
    """📋 Список → the account's card → 🗑 Убрать → 🗑 Да; the replies after the list."""
    state, msg = state or _State(), _CbMsg()
    asyncio.run(accounts.card.list_personal(msg, state, pool, personal))
    _press("acc_act", f"rm.{_key(state, 0)}", state, pool, personal, msg)
    _press("prm_ok", _key(state, 0), state, pool, personal, msg)
    return [text for text, _ in msg.replies[1:]]


def test_an_old_removal_confirmation_never_removes_another_account(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    removed = []

    async def remove_personal(personal, account):
        removed.append(account.path)
        return True

    monkeypatch.setattr(accounts.card.sign_in, "remove_personal", remove_personal)
    personal, _, _ = _personal(tmp_path)
    personal.full_sessions = {"personal_sessions/a.jsession": _Me(), "personal_sessions/b.jsession": _Me()}
    state, msg, pool = _State(), _CbMsg(), _free_pool()
    asyncio.run(accounts.card.list_personal(msg, state, pool, personal))
    old = _key(state, 1)
    _press("acc_act", f"rm.{old}", state, pool, personal, msg)
    # «Убрать b?» is shown; a personal account is added and the list is opened again: it is rebuilt
    personal.full_sessions = {"personal_sessions/0.jsession": _Me(), **personal.full_sessions}
    asyncio.run(accounts.card.list_personal(msg, state, pool, personal))

    _press("prm_ok", old, state, pool, personal, msg)  # «Да» on b's old confirmation
    assert removed == [] and "устарел" in msg.replies[-1][0]  # not a.jsession, now at index 1


def _free_pool():
    pool = _Pool([], _Storage())
    pool.busy = lambda path: False
    return pool


def test_personal_accounts_are_listed_without_connecting(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    personal, me, _ = _personal(tmp_path)
    msg = _CbMsg()
    asyncio.run(accounts.workers.accounts(msg, _State(), _Pool([], _Storage()), _Manager(free=True), personal))

    texts = [text for text, _ in msg.replies]
    assert any("Воркеров: <b>0</b>" in t for t in texts)  # shown with no worker too
    assert "👤 <b>Личные</b>" in texts[-1] and "me.jsession" in texts[-1] and not me.connected
    # its card, and ⬅️ back to the screen
    assert [b.text for row in msg.replies[-1][1].inline_keyboard for b in row] == ["👤 me.jsession", "⬅️ 🤖 Аккаунты"]


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
    json_file.save(scraped_files.SCRAPE_MARKER, {"1": {"account": path}})
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


# --- the countries of the numbers, each account's card ------------------------------------------

def test_the_list_says_the_numbers_countries(tmp_path, monkeypatch):
    """A row per country, the most first; a worker that didn't answer counts by its stored number."""
    monkeypatch.chdir(tmp_path)
    silent = _Client(fail=True)
    silent.path = "sessions/x.jsession"
    stored = ns(account=ns(account=ns(first_name="S", last_name=None, phone_number="+77011234567", user_id=3)))
    workers = [_Client(_me(first_name="A", last_name=None, id=1, username="a", phone="79991234567")),
               _Client(_me(first_name="B", last_name=None, id=2, username="b", phone="79161234567")), silent]
    msg = _Msg()
    asyncio.run(accounts.workers.accounts(msg, _State(), _Pool(workers, _Storage({silent.path: stored})),
                                  _Manager(free=True), None))
    assert "\n🌍 +7 Россия — 2 · +7 Казахстан — 1\n" in msg.replies[0]


def _workers_and_me(tmp_path):
    """A pool of one worker (stored in its .jsession, with a proxy), a personal account."""
    worker = _Client()
    worker.path = "sessions/w.jsession"
    stored = ns(account=ns(account=ns(first_name="Ann", last_name=None, username="ann", phone_number="79991234567",
                                      user_id=7), proxy=Proxy("socks5", "1.2.3.4", 1080)))
    storage = _Storage({worker.path: stored}, sessions=[worker])
    pool = _free_pool()
    pool.storage, pool._workers = storage, [worker]
    personal, me, _ = _personal(tmp_path)
    return pool, personal, me


def test_the_cards_are_offered_even_while_the_list_waits_for_a_task(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    pool, personal, _ = _workers_and_me(tmp_path)
    msg = _CbMsg()
    asyncio.run(accounts.workers.accounts(msg, _State(), pool, _Manager(free=False), personal))
    assert "Идёт задача" in msg.replies[0][0]
    assert "\n🌍 +7 Россия — 1\n" in msg.replies[0][0]  # its stored number: none is polled now
    # as listed: the workers, then the personal ones
    assert [b.text for row in msg.replies[-1][1].inline_keyboard for b in row] == ["🤖 Ann (@ann)", "👤 me.jsession",
                                                                                 "⬅️ 🤖 Аккаунты"]


def test_a_worker_s_card_is_from_what_is_stored_and_has_no_removal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    pool, personal, _ = _workers_and_me(tmp_path)
    state, msg = _State(), _CbMsg()
    asyncio.run(accounts.card.list_personal(msg, state, pool, personal))
    _press("acc", _key(state, 0), state, pool, personal, msg)
    text, markup = msg.replies[-1]
    # the number, id and file in <code>: a tap copies them
    assert text == ("<b>🤖 Воркер: Ann (@ann)</b>\n📱 <code>+79991234567</code> · 🆔 <code>7</code>\n"
                    "🌐 socks5://1.2.3.4:1080\n❔ не проверялся\n📄 <code>sessions/w.jsession</code>")
    # a worker is moved to another project (🤖 Аккаунты → ↪️ Перенести), never removed
    assert [b.text for row in markup.inline_keyboard for b in row] == ["🔑 Код входа", "⬅️ К списку"]

    _press("acc_act", f"rm.{_key(state, 0)}", state, pool, personal, msg)  # a forged 🗑 on a worker removes nothing
    assert "устарел" in msg.replies[-1][0]


def test_a_personal_card_offers_its_removal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    pool, personal, me = _workers_and_me(tmp_path)
    state, msg = _State(), _CbMsg()
    asyncio.run(accounts.card.list_personal(msg, state, pool, personal))
    _press("acc", _key(state, 1), state, pool, personal, msg)
    text, markup = msg.replies[-1]
    assert text.startswith("<b>👤 Личный (только для скрапа): me.jsession</b>") and not me.connected
    assert [b.text for row in markup.inline_keyboard for b in row] == ["🔑 Код входа", "🗑 Убрать", "⬅️ К списку"]

    _press("acc_act", f"list.{_key(state, 1)}", state, pool, personal, msg)  # ⬅️: the list again, in place
    assert msg.replies[-1][0].endswith("🗂 Карточка аккаунта — код входа, удаление личного:")


class _EditMsg(_CbMsg):
    def __init__(self):
        super().__init__()
        self.edits = []

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs.get("reply_markup")))

    async def edit_reply_markup(self, reply_markup=None):
        self.edits.append((None, reply_markup))


def test_the_codes_open_in_the_card_s_place_and_go_back_to_it(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    monkeypatch.chdir(tmp_path)

    async def fetch_codes(pool, client, path):
        return [("12345", datetime.now(timezone.utc))]
    monkeypatch.setattr(accounts.card.logins, "fetch_codes", fetch_codes)
    pool, personal, _ = _workers_and_me(tmp_path)
    state, msg = _State(), _EditMsg()
    asyncio.run(accounts.card.list_personal(msg, state, pool, personal))
    _press("acc_act", f"codes.{_key(state, 0)}", state, pool, personal, msg)
    text, markup = msg.edits[-1]
    assert "<code>1 2 3 4 5</code>" in text
    assert [b.text for row in markup.inline_keyboard for b in row] == ["🔄 Обновить", "⬅️ Назад"]
    _press("acc_act", f"card.{_key(state, 0)}", state, pool, personal, msg)
    assert msg.edits[-1][0].startswith("<b>🤖 Воркер: Ann (@ann)</b>")


def test_an_old_card_s_buttons_do_nothing_after_a_new_list(tmp_path, monkeypatch):
    """A new list rebuilds the indices: an older card's 🗑 or 🔑 would hit the account now at its index."""
    monkeypatch.chdir(tmp_path)
    read = []

    async def fetch_codes(pool, client, path):
        read.append(path)
        return []
    monkeypatch.setattr(accounts.card.logins, "fetch_codes", fetch_codes)
    pool, personal, me = _workers_and_me(tmp_path)
    state, msg = _State(), _EditMsg()
    asyncio.run(accounts.card.list_personal(msg, state, pool, personal))
    old = _key(state, 1)  # the personal account's card
    asyncio.run(accounts.card.list_personal(msg, state, pool, personal))  # 📋 Список again
    for action in ("rm", "codes", "card"):
        _press("acc_act", f"{action}.{old}", state, pool, personal, msg)
        assert "устарел" in msg.replies[-1][0]
    assert read == [] and msg.edits == [] and "prm_confirm" not in state.data


def test_the_cards_are_in_the_list_s_order(tmp_path, monkeypatch):
    """The workers as the list sorts them (the restricted last, by username), then the personal ones."""
    from modules import restricted_workers

    monkeypatch.chdir(tmp_path)
    workers = []
    for name, uid in (("b", 1), ("a", 2), ("c", 3)):
        client = _Client(_me(first_name=name, last_name=None, id=uid, username=name))
        client.path = f"sessions/{name}.jsession"
        workers.append(client)
    restricted_workers.save(["sessions/a.jsession"])
    stored = {c.path: ns(account=ns(account=ns(first_name=c._me.first_name, last_name=None,
                                               username=c._me.username, phone_number="79991234567", user_id=1)))
              for c in workers}
    personal, _, _ = _personal(tmp_path)
    msg = _CbMsg()
    asyncio.run(accounts.workers.accounts(msg, _State(), _Pool(workers, _Storage(stored, sessions=workers)),
                                  _Manager(free=True), personal))
    assert [b.text for row in msg.replies[-1][1].inline_keyboard for b in row] == \
        ["🤖 b (@b)", "🤖 c (@c)", "🤖 a (@a)", "👤 me.jsession", "⬅️ 🤖 Аккаунты"]  # ⬅️: back to its screen


def test_the_cards_of_many_accounts_are_paged(tmp_path, monkeypatch):
    """Telegram takes at most 100 buttons under a message: 130 accounts are 7 pages of 20."""
    monkeypatch.chdir(tmp_path)
    workers = []
    for i in range(130):
        client = _Client()
        client.path = f"sessions/w{i:03}.jsession"
        workers.append(client)
    pool = _free_pool()
    pool.storage, pool._workers = _Storage(sessions=workers), workers
    state, msg = _State(), _EditMsg()
    state.data["account_paths"] = ["sessions/other.jsession"]  # a scrape's «На каком аккаунте?» meanwhile
    asyncio.run(accounts.card.list_personal(msg, state, pool, None))
    texts = [b.text for row in msg.replies[-1][1].inline_keyboard for b in row]
    assert len(texts) == 24 and texts[0] == "🤖 w000.jsession" and texts[-3:] == ["📄 стр 1/7", "▶️", "⬅️ 🤖 Аккаунты"]
    assert state.data["account_paths"] == ["sessions/other.jsession"]  # the list keeps out of its way

    token = state.data["acc_token"]
    _press("acc", f"pg.1.{token}", state, pool, None, msg)  # ➡️: in place
    texts = [b.text for row in msg.edits[-1][1].inline_keyboard for b in row]
    assert texts[0] == "🤖 w020.jsession" and texts[-3] == "📄 стр 2/7"
    _press("acc", _key(state, 25), state, pool, None, msg)  # a card from the second page, in the list's place
    assert msg.edits[-1][0].startswith("<b>🤖 Воркер: w025.jsession</b>")

    asyncio.run(accounts.card.list_personal(msg, state, pool, None))  # 📋 Список again: the old ➡️ is stale
    _press("acc", f"pg.2.{token}", state, pool, None, msg)
    assert "устарел" in msg.replies[-1][0]
