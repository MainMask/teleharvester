"""🔥 Прогрев in the bot (bot/routers/warmup.py, bot/services/warmup_daily.py) and deleting a project's accounts."""
import asyncio
import datetime
import io
import os
import zipfile
from types import SimpleNamespace as ns

import pytest

from bot.callbacks import AccountBaseCB, NavCB, WarmupCB
from bot.routers import warmup as router
from bot.routers.accounts import base
from bot.services import warmup_daily
from bot.services.workspaces import delete_workers
from modules import restricted_workers, warmup
from modules.registry import WARMUP

import conftest


class _Message:
    def __init__(self):
        self.chat, self.edits, self.answers, self.documents = ns(id=55), [], [], []
        self.date, self.edit_date = datetime.datetime.now(datetime.timezone.utc), None

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs.get("reply_markup")))

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs.get("reply_markup")))

    async def answer_document(self, document, **kwargs):
        self.documents.append(document)


class _Callback:
    def __init__(self):
        self.message, self.alerts, self.bot = _Message(), [], None

    async def answer(self, text=None, **kwargs):
        if text:
            self.alerts.append(text)


class _Client:
    async def disconnect(self):
        pass


class _Storage:
    def __init__(self, names=()):
        self.home = conftest.home()
        self.directory = os.path.join(self.home, "sessions")
        os.makedirs(self.directory, exist_ok=True)
        self.jsessions_paths, self.full_sessions, self.forgotten = {}, {}, []
        for name in names:
            path = os.path.join(self.directory, name)
            open(path, "w").close()
            self.full_sessions[path] = _Client()

    @property
    def sessions(self):
        return list(self.full_sessions.values())

    def get_session_path(self, client):
        return next(path for path, session in self.full_sessions.items() if session is client)

    def forget_session(self, path):
        self.forgotten.append(path)
        self.full_sessions.pop(path, None)


class _Pool:
    def __init__(self, storage, busy=(), scraping=()):
        self.storage, self._busy, self._scraping = storage, set(busy), set(scraping)

    def count(self):
        return len(self.storage.full_sessions)

    def busy(self, path):
        return path in self._busy

    def scraped(self, path):
        return path in self._scraping


def _texts(markup):
    return [button.text for row in markup.inline_keyboard for button in row]


IDLE = ns(active=False, label="")


def test_a_setting_s_button_shows_its_next_value_in_place_and_daily_reports_to_its_chat():
    pool, callback = _Pool(_Storage(["a.session"])), _Callback()
    assert "⏸ Прогрев не идёт · 🔁 Автозапуск: выкл" in router._warmup_view(pool, IDLE, 3)[0]
    asyncio.run(router.setting_next(callback, WarmupCB(action="set", value="daily", project=3), pool, IDLE,
                                    ns(project_id=3)))
    text, markup = callback.message.edits[-1]
    assert "🔁 Ежедневно: вкл" in _texts(markup) and "🔥 Прогрев" in text and "✅ Прогрето 0 из 1\n" in text
    assert "🔄 Обновить" in _texts(markup) and warmup.load_settings(conftest.home())["chat"] == 55
    assert any(f"🔁 Автозапуск: {when}" in text for when in ("сегодня ~", "завтра"))
    # a step's button: the steps' screen again, the step's mode cycled
    asyncio.run(router.setting_next(callback, WarmupCB(action="set", value="bio", project=3), pool, IDLE,
                                    ns(project_id=3)))
    assert "🛡 📝 Bio" in _texts(callback.message.edits[-1][1])
    back = callback.message.edits[-1][1].inline_keyboard[-1][0]
    assert NavCB.unpack(back.callback_data) == NavCB(screen=WARMUP, project=3)


def test_an_old_message_s_days_button_says_it_is_stale():
    # 📅 Дней was a cycling button ("set"); now it is typed ("number"): an older message's tap is said in a pop-up
    from bot.routers._common import STALE_BUTTON

    pool, callback = _Pool(_Storage(["a.session"])), _Callback()
    asyncio.run(router.setting_next(callback, WarmupCB(action="set", value="days", project=3), pool, IDLE,
                                    ns(project_id=3)))
    assert callback.alerts == [STALE_BUTTON] and callback.message.edits == []
    assert warmup.load_settings(conftest.home())["days"] == warmup.DEFAULTS["days"]


def test_every_step_s_random_button_cycles_its_own_value_and_leaves_the_step_as_it_is():
    pool, callback = _Pool(_Storage(["a.session"])), _Callback()
    for step, field in warmup.RANDOM.items():
        before = warmup.load_settings(conftest.home())
        asyncio.run(router.setting_next(callback, WarmupCB(action="set", value=field, project=3), pool, IDLE,
                                        ns(project_id=3)))
        after = warmup.load_settings(conftest.home())
        values = warmup.CYCLES[field]
        assert after[field] == values[(values.index(before[field]) + 1) % len(values)], step
        assert after["steps"][step] == before["steps"][step], step
    # the channels a day: from a few up to 5–10
    labels = [router._random_label("channels_count", value) for value in warmup.CYCLES["channels_count"]]
    assert labels[0] == "🎲 0–3 в день" and labels[-1] == "🎲 5–10 в день"


def test_with_nobody_to_warm_today_the_button_says_so_and_runs_nothing():
    storage = _Storage(["a.session"])
    warmup.update(next(iter(storage.full_sessions)), status=warmup.WARMED)
    callback, launched = _Callback(), []
    manager = ns(run=lambda *a, **k: launched.append(a))
    asyncio.run(router.warmup_run(callback, _Pool(storage), {}, manager))
    assert callback.alerts == [router.NOTHING_DUE] and launched == []


def test_deleting_skips_the_busy_and_drops_the_state_of_the_rest():
    storage = _Storage(["a.session", "b.session"])
    a, b = sorted(storage.full_sessions)
    restricted_workers.save([a, b])
    warmup.update(a, status=warmup.FAILED)
    deleted, failed = asyncio.run(delete_workers(_Pool(storage, busy=[b]), [a, b]))
    assert len(deleted) == 1 and [why for _, why in failed] == [
        "занят задачей или незавершённым скрапом — удалите после их окончания"]
    assert not os.path.exists(a) and os.path.exists(b) and storage.forgotten == [a]
    assert restricted_workers.load() == [b] and warmup.load_state() == {}


def test_deleting_a_dead_account_drops_its_warmup_kept_by_its_user_id():
    """A dead one (sessions/inactive/) is no session of the storage's: its user id is read from its file."""
    storage = _Storage()
    dead = os.path.join(storage.directory, "inactive", "79991234567.jsession")
    os.makedirs(os.path.dirname(dead))
    _jsession(dead)  # user id 7
    warmup.update("7", status=warmup.WARMED)  # as it was warmed while alive
    deleted, failed = asyncio.run(delete_workers(_Pool(storage), [dead]))
    assert len(deleted) == 1 and not failed and not os.path.exists(dead)
    assert warmup.load_state() == {}


def test_a_category_s_zip_holds_its_session_files():
    storage = _Storage(["a.session", "b.session"])
    a, _ = sorted(storage.full_sessions)
    warmup.update(a, status=warmup.WARMED)
    callback = _Callback()
    asyncio.run(base.category_zip(callback, AccountBaseCB(action="zip", value=warmup.WARMED, project=3),
                                  _Pool(storage), ns(project_id=3)))
    # the format first, in place: the archive not sent yet
    text, markup = callback.message.edits[-1]
    assert "в каком формате?" in text and callback.message.documents == []
    assert _texts(markup) == ["📦 .session + .json", "📦 .jsession", "✖️ Отмена"]
    asyncio.run(base.category_zip(callback, AccountBaseCB(action="zip", value=warmup.WARMED, project=3,
                                                          fmt="jsession"), _Pool(storage), ns(project_id=3)))
    document = callback.message.documents[0]
    assert zipfile.ZipFile(io.BytesIO(document.data)).namelist() == ["a.session"]


def _key() -> str:
    """A real key, as a StringSession."""
    from telethon.crypto import AuthKey
    from telethon.sessions import StringSession

    session = StringSession()
    session.set_dc(2, "149.154.167.51", 443)
    session.auth_key = AuthKey(os.urandom(256))
    return session.save()


def _jsession(path, password="pw"):
    """A worker's .jsession with a real key; the key."""
    from modules.types.account import Account
    from modules.types.account_settings import AccountSettings
    from modules.types.application import Application

    key = _key()
    AccountSettings(key, Account("Ann", None, 7, 0.0, "79991234567", "ann"),
                    Application(2040, "hash", "PC 64bit", "5.1", "Windows 10", "tdesktop", "ru"),
                    None, password).save(path)
    return key


def test_a_zip_as_session_and_json_reads_back_as_the_same_account():
    """What sellers give, what ➕ Добавить → 📥 Из файлов (modules/session_import.py) reads back."""
    import json
    import tempfile

    from modules import session_import

    storage = _Storage()
    path = os.path.join(storage.directory, "79991234567.jsession")
    key = _jsession(path)
    storage.full_sessions[path] = _Client()
    callback = _Callback()
    asyncio.run(base.category_zip(callback, AccountBaseCB(action="zip", value="all", project=3, fmt="session"),
                                  _Pool(storage), ns(project_id=3)))
    archive = zipfile.ZipFile(io.BytesIO(callback.message.documents[0].data))
    assert archive.namelist() == ["79991234567.session", "79991234567.json"]
    with tempfile.TemporaryDirectory() as tmp:
        archive.extractall(tmp)
        [(session, meta)] = session_import.collect(tmp)
        assert session_import.read_key(session) == key
    assert meta == json.loads(archive.read("79991234567.json"))
    assert (meta["app_id"], meta["device"], meta["twoFA"], meta["user_id"]) == (2040, "PC 64bit", "pw", 7)


def test_a_string_session_worker_s_zip_is_a_telethon_sqlite_session_too():
    """A .session worker is a StringSession (SessionsStorage): given as Telethon's SQLite, as the format says;
    an empty one is no session and is told as not read."""
    import tempfile

    from modules import session_import

    storage = _Storage(["empty.session"])
    path = os.path.join(storage.directory, "79990000001.session")
    key = _key()
    with open(path, "w") as fileobj:
        fileobj.write(key)
    storage.full_sessions[path] = _Client()
    callback = _Callback()
    asyncio.run(base.category_zip(callback, AccountBaseCB(action="zip", value="all", project=3, fmt="session"),
                                  _Pool(storage), ns(project_id=3)))
    archive = zipfile.ZipFile(io.BytesIO(callback.message.documents[0].data))
    assert archive.namelist() == ["79990000001.session"]  # the empty one: not read, left out
    with tempfile.TemporaryDirectory() as tmp:
        archive.extractall(tmp)
        [(session, meta)] = session_import.collect(tmp)
        assert session_import.read_key(session) == key and meta == {}


def test_the_daily_run_starts_once_its_time_has_come_and_the_slot_is_free(monkeypatch):
    storage = _Storage(["a.session"])
    settings = warmup.load_settings(conftest.home())
    settings["daily"] = True
    warmup.save_settings(conftest.home(), settings)
    runs = []

    class _Manager:
        active = False

        async def run(self, bot, chat, *args):
            runs.append(chat)
            return True

    workspace = ns(project_id=1, pool=_Pool(storage), manager=_Manager(), functions={"WarmupFunc": lambda: "func"})
    workspaces = ns(all=lambda: [workspace])
    now = datetime.datetime(2026, 1, 1, 12)
    monkeypatch.setitem(warmup_daily._planned, 1, now.replace(hour=13))
    asyncio.run(warmup_daily.run_due(None, workspaces, {9}, now))
    assert runs == []  # not its time yet
    asyncio.run(warmup_daily.run_due(None, workspaces, {9}, now.replace(hour=14)))
    asyncio.run(warmup_daily.run_due(None, workspaces, {9}, now.replace(hour=15)))
    assert runs == [9] and warmup.load_settings(conftest.home())["auto_day"] == "2026-01-01"


def test_a_setting_changed_while_the_daily_run_starts_is_kept(monkeypatch):
    storage = _Storage(["a.session"])
    settings = warmup.load_settings(conftest.home())
    settings["daily"] = True
    warmup.save_settings(conftest.home(), settings)

    class _Manager:
        active = False

        async def run(self, *args):  # the operator taps 📅 Дней while the run's message is being sent
            warmup.save_settings(conftest.home(), {**warmup.load_settings(conftest.home()), "days": 7})
            return True

    workspace = ns(project_id=1, pool=_Pool(storage), manager=_Manager(), functions={"WarmupFunc": lambda: "func"})
    now = datetime.datetime(2026, 1, 1, 14)
    monkeypatch.setitem(warmup_daily._planned, 1, now.replace(hour=13))
    asyncio.run(warmup_daily.run_due(None, ns(all=lambda: [workspace]), {9}, now))
    saved = warmup.load_settings(conftest.home())
    assert saved["days"] == 7 and saved["auto_day"] == "2026-01-01"


def test_a_worker_the_scraper_runs_on_is_not_due_for_the_warmup(monkeypatch):
    """The warmup runs without it (WorkerPool.delegate): due alone, nothing runs — no empty job."""
    storage = _Storage(["a.session"])
    pool = _Pool(storage, scraping=list(storage.full_sessions))
    callback, launched = _Callback(), []
    manager = ns(run=lambda *a, **k: launched.append(a))
    asyncio.run(router.warmup_run(callback, pool, {}, manager))
    assert callback.alerts == [router.NOTHING_DUE] and launched == []

    settings = warmup.load_settings(conftest.home())
    settings["daily"] = True
    warmup.save_settings(conftest.home(), settings)

    class _Manager:
        active = False

        async def run(self, *args):
            launched.append(args)
            return True

    workspace = ns(project_id=1, pool=pool, manager=_Manager(), functions={"WarmupFunc": lambda: "func"})
    now = datetime.datetime(2026, 1, 1, 14)
    monkeypatch.setitem(warmup_daily._planned, 1, now.replace(hour=13))
    asyncio.run(warmup_daily.run_due(None, ns(all=lambda: [workspace]), {9}, now))
    assert launched == []


def test_the_daily_run_past_its_window_waits_for_the_next_day(monkeypatch):
    storage = _Storage(["a.session"])
    settings = warmup.load_settings(conftest.home())
    settings["daily"] = True
    warmup.save_settings(conftest.home(), settings)
    runs = []

    class _Manager:
        active = False

        async def run(self, bot, chat, *args):
            runs.append(chat)
            return True

    workspace = ns(project_id=1, pool=_Pool(storage), manager=_Manager(), functions={"WarmupFunc": lambda: "func"})
    workspaces = ns(all=lambda: [workspace])
    now = datetime.datetime(2026, 1, 1, warmup_daily.LAST_HOUR, 30)  # its slot was busy until now, or a late start
    monkeypatch.setitem(warmup_daily._planned, 1, now.replace(hour=13))
    asyncio.run(warmup_daily.run_due(None, workspaces, {9}, now))
    assert runs == [] and warmup.load_settings(conftest.home())["auto_day"] == ""
    asyncio.run(warmup_daily.run_due(None, workspaces, {9}, datetime.datetime(2026, 1, 2, 23, 59)))
    assert runs == []  # the next day's time is picked anew, within its window


def test_the_reset_and_a_category_s_deletion_are_asked_in_their_screen_s_place():
    storage = _Storage(["a.session"])
    warmup.update(os.path.join(storage.directory, "a.session"), day=2)
    reset = _Callback()
    asyncio.run(router.reset_ask(reset, _Pool(storage), ns(project_id=3)))
    [(text, markup)] = reset.message.edits  # in 🔥's place, no new message
    assert text.startswith("♻️ Сбросить прогрев") and reset.message.answers == []
    assert NavCB.unpack(markup.inline_keyboard[0][1].callback_data) == NavCB(screen=WARMUP, project=3)  # ↩️

    warmup.forget([os.path.join(storage.directory, "a.session")])  # 🆕 again
    delete = _Callback()
    asyncio.run(base.delete_ask(delete, AccountBaseCB(action="del", value=warmup.NEW, project=3), _State(),
                                _Pool(storage), ns(project_id=3)))
    [(text, markup)] = delete.message.edits
    assert text.startswith("🗑 Удалить файлы сессий категории 🆕 Новые (1)") and delete.message.answers == []
    # ↩️ opens the category again in its place
    assert AccountBaseCB.unpack(markup.inline_keyboard[0][1].callback_data) == AccountBaseCB(
        action="cat", value=warmup.NEW, project=3)


def test_the_reset_of_accounts_never_warmed_is_not_asked_but_said_in_a_pop_up():
    reset = _Callback()
    asyncio.run(router.reset_ask(reset, _Pool(_Storage(["a.session"])), ns(project_id=3)))
    assert reset.alerts == ["Сбрасывать нечего: аккаунты проекта ещё не прогревались."]
    assert reset.message.edits == []


def test_the_reset_runs_on_a_yes_asked_in_the_screen_s_place():
    """Telegram gives the edited question's edit_date as a unix time: «Да» compares it, not breaks on it."""
    from bot.services.jobs import JobManager

    storage = _Storage(["a.session"])
    path = os.path.join(storage.directory, "a.session")
    warmup.update(path, day=2)
    yes = _Callback()
    yes.message.edit_date = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    asyncio.run(router.reset_run(yes, _Pool(storage), JobManager(), ns(project_id=3)))
    assert yes.message.edits[0][0] == "✅ Прогрев сброшен: аккаунтов 1 начнут с первого дня."
    assert path not in warmup.load_state()


def test_the_deletion_of_a_category_emptied_meanwhile_is_not_asked_but_said_in_a_pop_up():
    """Its 🗑 is on a category shown before: its accounts moved to another one since."""
    storage = _Storage(["a.session"])
    warmup.update(os.path.join(storage.directory, "a.session"), status=warmup.WARMED)  # 🆕 no more
    delete = _Callback()
    asyncio.run(base.delete_ask(delete, AccountBaseCB(action="del", value=warmup.NEW, project=3), _State(),
                                _Pool(storage), ns(project_id=3)))
    assert delete.alerts == [base.EMPTY_CATEGORY] and delete.message.edits == []


def test_a_category_s_deletion_deletes_only_the_accounts_it_asked_about():
    """Between the question and «Да» a warmup may move more accounts into the category: they stay."""
    storage, state = _Storage(["a.session"]), _State()
    pool, project = _Pool(storage), ns(project_id=3)
    asyncio.run(base.delete_ask(_Callback(), AccountBaseCB(action="del", value=warmup.NEW, project=3), state, pool,
                                project))
    [shown] = storage.full_sessions
    later = os.path.join(storage.directory, "b.session")  # new since the question: in 🆕 Новые too
    open(later, "w").close()
    storage.full_sessions[later] = _Client()
    confirm = _Callback()
    asyncio.run(base.delete_run(confirm, AccountBaseCB(action="delok", value=warmup.NEW, project=3), state, pool,
                                project))
    assert not os.path.exists(shown) and os.path.exists(later)
    assert confirm.message.edits[0][0].startswith("🗑 🆕 Новые: удалено 1")
    again = _Callback()  # the same «Да» a second time: asked about nothing now
    asyncio.run(base.delete_run(again, AccountBaseCB(action="delok", value=warmup.NEW, project=3), state, pool,
                                project))
    assert again.alerts == ["Подтверждение устарело — откройте категорию заново."] and os.path.exists(later)


def test_a_confirmation_asked_in_an_old_screen_s_place_is_as_old_as_its_question():
    from bot.routers._common import confirm_expired

    now = datetime.datetime.now(datetime.timezone.utc)
    screen = ns(date=now - datetime.timedelta(hours=2), edit_date=int(now.timestamp()) - 5)  # Telegram's: a unix time
    assert not confirm_expired(screen)  # the screen is old, the question in its place is not
    assert confirm_expired(ns(date=screen.date, edit_date=int(now.timestamp()) - 3600))
    assert confirm_expired(ns(date=screen.date, edit_date=None))  # never edited: as old as it was sent


def test_the_warmup_s_run_comes_first_marked_risky_in_the_text_and_its_region_is_the_project_s():
    from bot.services.jobs import JobManager

    text, markup = router._warmup_view(_Pool(_Storage(["a.session"])), JobManager(), 0)
    assert text.startswith("<b>🔥 Прогрев</b>\n") and "<b>▶️ Прогреть</b> ⚠️" in text
    # its own buttons between its function and the screen it opens, in the text in the same order; 🔄 by ⬅️
    assert _texts(markup) == ["▶️ Прогреть", "📅 Дней: 3", "🧵 Потоков: 1", "🔁 Ежедневно: выкл",
                              "♻️ Сбросить прогрев", "🧩 Шаги ›", "🔄 Обновить", "⬅️ Назад"]
    assert [b.text for b in markup.inline_keyboard[-1]] == ["🔄 Обновить", "⬅️ Назад"]
    assert text.index("▶️ Прогреть") < text.index("<b>📅 Дней</b>") < text.index("<b>🧩 Шаги</b>")
    # 🌍 is 🛠 Управление's: here only said
    assert "🌍 Регион: СНГ (RU) — меняется в «🛠 Управление»" in text


class _State:
    def __init__(self):
        self.data = {}

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kw):
        self.data.update(kw)


def test_the_list_s_categories_open_their_accounts_cards_and_a_card_goes_back_to_its_category():
    from bot.callbacks import ChoiceCB
    from bot.routers.accounts import card

    storage = _Storage(["a.session", "b.session"])
    a, _ = sorted(storage.full_sessions)
    warmup.update(a, status=warmup.WARMED)
    pool, state, screen, workspace = _Pool(storage), _State(), _Message(), ns(project_id=3)
    asyncio.run(base.list_screen(screen, state, False, pool, workspace, None))
    # 📋 Список: its checks (with workers), the categories with accounts only, the whole base's ZIP, 🔄 and ⬅️
    assert _texts(screen.answers[0][1]) == ["📡 Опросить", "🩺 SpamBot", "🆕 Новые · 1", "✅ Прогреты · 1",
                                            "📦 Все аккаунты", "🔄 Обновить", "⬅️ Назад"]
    assert "<b>Категории</b>" in screen.answers[0][0]

    callback = _Callback()
    asyncio.run(base.category_open(callback, AccountBaseCB(action="cat", value=warmup.WARMED, project=3), state, pool,
                                   workspace, None))
    text, markup = callback.message.edits[-1]  # in the screen's place
    assert "<b>✅ Прогреты</b>: 1\n\n• a.session" in text
    assert _texts(markup) == ["🤖 a.session", "📦 Скачать ZIP", "🗑 Удалить", "⬅️ Назад"]
    key = f"0.{state.data['acc_token']}"
    asyncio.run(card.account_card(callback, ChoiceCB(scope="acc", value=key), state, pool, None))
    assert callback.message.edits[-1][0].startswith("<b>🤖 Воркер: a.session</b>")
    asyncio.run(card.account_action(callback, ChoiceCB(scope="acc_act", value=f"list.{key}"), state, pool, None))
    assert callback.message.edits[-1] == (text, markup)  # ⬅️ Назад: the category again, its buttons too


def test_the_personal_accounts_are_a_button_of_the_list_with_their_cards():
    me = ns()
    personal = ns(sessions=[me], jsessions_paths={}, get_session_path=lambda client: "personal_sessions/me.session")
    pool, state, screen, workspace = _Pool(_Storage()), _State(), _Message(), ns(project_id=3)
    asyncio.run(base.list_screen(screen, state, False, pool, workspace, personal))
    # no file: nothing to zip, no category
    texts, text = _texts(screen.answers[0][1]), screen.answers[0][0]
    assert texts == ["👤 Личные · 1", "🔄 Обновить", "⬅️ Назад"] and "Категории" not in text \
        and "Аккаунтов нет" not in text
    callback = _Callback()
    asyncio.run(base.category_open(callback, AccountBaseCB(action="cat", value=base.PERSONAL, project=3), state, pool,
                                   workspace, personal))
    text, markup = callback.message.edits[-1]
    assert "👤 <b>Личные</b>" in text and _texts(markup) == ["👤 me.session", "⬅️ Назад"]


def test_an_empty_list_says_where_accounts_are_added():
    pool, screen = _Pool(_Storage()), _Message()
    asyncio.run(base.list_screen(screen, _State(), False, pool, ns(project_id=3), None))
    text, markup = screen.answers[0]
    # no worker: nothing to poll or check
    assert "Аккаунтов нет — добавьте их: «🤖 Аккаунты» → «➕ Добавить»." in text
    assert _texts(markup) == ["🔄 Обновить", "⬅️ Назад"] and "SpamBot" not in text


class _FormState(_State):
    def __init__(self):
        super().__init__()
        self.state = None

    async def set_state(self, state):
        self.state = state

    async def clear(self):
        self.state, self.data = None, {}


class _Typed(_Message):
    def __init__(self, text):
        super().__init__()
        self.text = text


@pytest.mark.parametrize("field, name, current", [("days", "📅 Дней", 3), ("threads", "🧵 Потоков", 1)])
def test_the_days_and_the_threads_are_typed_and_any_number_from_one_is_taken(field, name, current):
    from bot.services.jobs import JobManager
    from bot.states import SetWarmupNumber, origin

    pool, state, callback = _Pool(_Storage(["a.session"])), _FormState(), _Callback()
    assert f"{name}: {current}" in _texts(router._warmup_view(pool, JobManager(), 3)[1])
    asyncio.run(router.number_ask(callback, WarmupCB(action="number", value=field, project=3), state, pool))
    prompt, _ = callback.message.answers[-1]
    assert prompt.startswith(f"{name} — сейчас <b>{current}</b>") and state.state == SetWarmupNumber.value
    assert origin(SetWarmupNumber.value.state, {}) == WARMUP  # its ✖️: back to 🔥 Прогрев

    for wrong in ("0", "-3", "много", "2.5"):
        typed = _Typed(wrong)
        asyncio.run(router.number_typed(typed, state, pool, JobManager(), ns(project_id=3)))
        assert typed.answers[-1][0] == "Нужно целое число от 1. Пришлите ещё раз:" and state.state is not None

    typed = _Typed("150")
    asyncio.run(router.number_typed(typed, state, pool, JobManager(), ns(project_id=3)))
    text, markup = typed.answers[-1]
    assert text.startswith(f"✅ {name}: 150.") and f"{name}: 150" in _texts(markup) and state.state is None
    assert warmup.load_settings(conftest.home())[field] == 150
