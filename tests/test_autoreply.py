"""Offline tests for the auto-reply to PM-mailing replies (bot/services/autoreply.py)."""

import asyncio
import json
import types as pytypes
from datetime import datetime

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from telethon import types

from bot.services import autoreply


TEXT = "Для связи пишите @MainMask"


@pytest.fixture(autouse=True)
def _isolated_replied(monkeypatch, tmp_path):
    monkeypatch.setattr(autoreply, "REPLIED_PATH", str(tmp_path / "auto_replies.json"))
    monkeypatch.setattr(autoreply, "_not_mailing", set())


def user(user_id=1, **kw):
    fields = dict(id=user_id, first_name="Ann", last_name=None, username="ann",
                  bot=False, is_self=False, deleted=False, phone=None)
    fields.update(kw)
    return types.User(**fields)


def message(text="hi", out=False, msg_id=10):
    return pytypes.SimpleNamespace(out=out, message=text, id=msg_id)


def dialog(entity, unread=1, out=False, text="hi", msg_id=10, history=None):
    """history: the chat's messages, newest first (default: just the last one)."""
    last = message(text, out, msg_id)
    item = pytypes.SimpleNamespace(entity=entity, unread_count=unread, message=last)
    item.history = history if history is not None else [last]
    return item


class FakeClient:
    def __init__(self, dialogs, wrote_first=True, fail=None):
        self.dialogs = dialogs
        self.by_id = {d.entity.id: d for d in dialogs}
        self.wrote_first = wrote_first
        self.fail = fail
        self.sent = []
        self.parse_modes = []
        self.read = []
        self.searches = 0
        self.connected = False
        self.session = pytypes.SimpleNamespace(_entities={1})

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def iter_dialogs(self, limit):
        if self.fail:
            raise self.fail
        for item in self.dialogs:
            yield item

    async def get_messages(self, entity, limit, from_user=None):
        if from_user is not None:
            assert from_user == "me"
            self.searches += 1
            return ["msg"] if self.wrote_first else []
        return self.by_id[entity.id].history[:limit]

    async def send_message(self, entity, text, parse_mode="md"):
        self.sent.append((entity.id, text))
        self.parse_modes.append(parse_mode)

    async def send_read_acknowledge(self, entity, max_id):
        self.read.append((entity.id, max_id))


class FakePool:
    def __init__(self, clients, busy=(), scraping=None):
        self.workers = list(clients)
        self._paths = {id(c): f"sessions/w{i}.jsession" for i, c in enumerate(clients)}
        self._busy = set(busy)
        self.polling = None
        self.scraping = pytypes.SimpleNamespace(path=scraping) if scraping else None
        self.storage = pytypes.SimpleNamespace(
            get_session_path=lambda c: self._paths[id(c)],
            usernames={},
        )

    def busy(self, path):  # as WorkerPool.busy
        return path in self._busy or path == self.polling


def run_poll(pool, replied=None):
    notes = []

    async def notify(text, url):
        notes.append((text, url))
        return True

    replied = {} if replied is None else replied
    asyncio.run(autoreply.poll_once(pool, TEXT, replied, notify))
    return replied, notes


def test_answers_mailing_reply_saves_and_notifies():
    client = FakeClient([dialog(user(5), text="а что за предложение?", msg_id=42)])
    replied, notes = run_poll(FakePool([client]))

    assert client.sent == [(5, TEXT)]
    assert client.read == [(5, 42)]
    assert list(replied) == ["sessions/w0.jsession:5"]
    with open(autoreply.REPLIED_PATH, encoding="utf-8") as fileobj:
        assert list(json.load(fileobj)) == ["sessions/w0.jsession:5"]
    assert len(notes) == 1
    text, url = notes[0]
    assert "автоответ отправлен" in text and "Ann (@ann)" in text and "w0.jsession" in text
    assert "а что за предложение?" in text and url == "https://t.me/ann"


def test_reply_text_goes_as_typed_not_parsed_as_markdown():
    client = FakeClient([dialog(user(5))])
    run_poll(FakePool([client]))
    assert client.parse_modes == [None]  # "**", "__", "[x](y)" in the text stay as they are


def test_forwards_every_unread_message_oldest_first():
    history = [message("третье", msg_id=3), message("второе", msg_id=2), message("первое", msg_id=1)]
    client = FakeClient([dialog(user(5), unread=3, history=history, msg_id=3)])
    _, notes = run_poll(FakePool([client]))
    assert notes[0][0].endswith("первое\nвторое\nтретье")


def test_more_unread_than_forwarded_says_how_many_more():
    history = [message(f"m{i}", msg_id=i) for i in range(15, 0, -1)]
    client = FakeClient([dialog(user(5), unread=15, history=history, msg_id=15)])
    _, notes = run_poll(FakePool([client]))
    text = notes[0][0]
    assert "…и ещё 5 раньше" in text and "m6\n" in text and "m5\n" not in text and text.endswith("m15")


def test_answered_person_writing_again_is_forwarded_without_another_reply():
    first = FakeClient([dialog(user(5), text="привет")])
    replied, _ = run_poll(FakePool([first]))

    again = FakeClient([dialog(user(5), text="а подробнее?", msg_id=11)], wrote_first=False)
    _, notes = run_poll(FakePool([again]), replied)

    assert again.sent == []
    assert again.read == [(5, 11)]
    assert "Новое сообщение" in notes[0][0] and "а подробнее?" in notes[0][0]


def test_read_chat_of_an_answered_person_is_left_alone():
    client = FakeClient([dialog(user(5))])
    replied, _ = run_poll(FakePool([client]))
    _, notes = run_poll(FakePool([FakeClient([dialog(user(5), unread=0)])]), replied)
    assert notes == []


@pytest.mark.parametrize("item", [
    dialog(user(5, bot=True)),
    dialog(user(autoreply.SERVICE_ID)),
    dialog(user(5, is_self=True)),
    dialog(user(5, deleted=True)),
    dialog(user(5), unread=0),
    dialog(user(5), out=True),
    dialog(types.Channel(id=7, title="c", photo=types.ChatPhotoEmpty(), date=None)),
    dialog(types.Chat(id=8, title="g", photo=types.ChatPhotoEmpty(), participants_count=2,
                      date=None, version=1)),
])
def test_skips_what_is_not_a_mailing_reply(item):
    client = FakeClient([item])
    replied, notes = run_poll(FakePool([client]))
    assert client.sent == [] and replied == {} and notes == []


def test_skips_people_the_worker_did_not_write_first():
    client = FakeClient([dialog(user(5))], wrote_first=False)
    replied, _ = run_poll(FakePool([client]))
    assert client.sent == [] and replied == {}


def test_notified_even_if_read_mark_fails():
    client = FakeClient([dialog(user(5))])

    async def dropped(entity, max_id):
        raise ConnectionError("a job disconnected it")

    client.send_read_acknowledge = dropped
    replied, notes = run_poll(FakePool([client]))

    assert client.sent == [(5, TEXT)] and list(replied) and len(notes) == 1


def test_failed_reply_is_still_forwarded_and_the_next_chat_handled():
    client = FakeClient([dialog(user(5), text="первый", msg_id=7), dialog(user(6), text="второй", msg_id=8)])
    real_send = client.send_message

    async def send(entity, text, **kwargs):
        if entity.id == 5:
            raise ValueError("USER_IS_BLOCKED")
        await real_send(entity, text, **kwargs)

    client.send_message = send
    replied, notes = run_poll(FakePool([client]))

    assert client.sent == [(6, TEXT)]
    assert list(replied) == ["sessions/w0.jsession:6"]  # 5 gets another try when they write again
    assert client.read == [(5, 7), (6, 8)]
    assert "НЕ отправлен" in notes[0][0] and "USER_IS_BLOCKED" in notes[0][0] and "первый" in notes[0][0]
    assert "автоответ отправлен" in notes[1][0] and "второй" in notes[1][0]


def test_one_failing_chat_does_not_stop_the_worker():
    client = FakeClient([dialog(user(5), msg_id=7), dialog(user(6), msg_id=8)])
    real_history = client.get_messages

    async def get_messages(entity, limit, from_user=None):
        if entity.id == 5 and from_user is None:
            raise ValueError("broken chat")
        return await real_history(entity, limit, from_user=from_user)

    client.get_messages = get_messages
    replied, notes = run_poll(FakePool([client]))

    assert client.sent == [(6, TEXT)] and list(replied) == ["sessions/w0.jsession:6"] and len(notes) == 1


def test_stranger_chat_is_searched_again_only_after_a_new_message():
    client = FakeClient([dialog(user(5), msg_id=10)], wrote_first=False)
    pool = FakePool([client])
    run_poll(pool)
    run_poll(pool)
    assert client.searches == 1

    client.dialogs[0].message = message("ещё", msg_id=11)
    run_poll(pool)
    assert client.searches == 2 and client.sent == []


def test_stranger_chats_memo_stays_bounded(monkeypatch):
    monkeypatch.setattr(autoreply, "NOT_MAILING_LIMIT", 2)
    client = FakeClient([dialog(user(5), msg_id=1), dialog(user(6), msg_id=1), dialog(user(7), msg_id=1)],
                        wrote_first=False)
    run_poll(FakePool([client]))
    assert len(autoreply._not_mailing) == 1  # cleared at the limit, then the newest one kept


def test_media_reply_is_notified_as_media():
    client = FakeClient([dialog(user(5), text="")])
    _, notes = run_poll(FakePool([client]))
    assert "[медиа]" in notes[0][0]


def test_phone_and_id_link_without_username():
    client = FakeClient([dialog(user(5, username=None, phone="79990001122"))])
    _, notes = run_poll(FakePool([client]))
    text, url = notes[0]
    assert "Ann (id 5), 📱 +79990001122" in text and url == "tg://user?id=5"


class FakeBot:
    def __init__(self, reject_buttons=False, floods=0):
        self.reject_buttons = reject_buttons
        self.floods = floods
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        if self.floods:
            self.floods -= 1
            raise TelegramRetryAfter(method=None, message="Too Many Requests", retry_after=0)
        if reply_markup is not None and self.reject_buttons:
            raise TelegramBadRequest(method=None, message="BUTTON_USER_PRIVACY_RESTRICTED")
        self.sent.append((chat_id, text, reply_markup))


def test_notify_sends_a_profile_button():
    bot = FakeBot()
    asyncio.run(autoreply.make_notify(bot, {1})("hi", "https://t.me/ann"))
    (chat_id, text, markup), = bot.sent
    assert chat_id == 1 and markup.inline_keyboard[0][0].url == "https://t.me/ann"


def test_notify_falls_back_to_no_button():
    bot = FakeBot(reject_buttons=True)
    asyncio.run(autoreply.make_notify(bot, {1})("hi", "tg://user?id=5"))
    assert bot.sent == [(1, "hi", None)]


def test_notify_waits_out_the_flood_limit():
    bot = FakeBot(floods=2)
    asyncio.run(autoreply.make_notify(bot, {1})("hi", "https://t.me/ann"))
    assert len(bot.sent) == 1


def test_reply_stats():
    replied = {
        "sessions/a.jsession:1": "2026-10-06 11:00:00",
        "sessions/a.jsession:2": "2026-10-03 12:00:00",
        "sessions/b.jsession:3": "2026-09-01 12:00:00",
    }
    stats = autoreply.reply_stats(replied, datetime(2026, 10, 6, 12, 0, 0))
    assert (stats["total"], stats["day"], stats["week"]) == (3, 1, 2)
    assert stats["per_worker"] == {"sessions/a.jsession": 2, "sessions/b.jsession": 1}


def test_free_worker_disconnected_busy_one_kept():
    free, busy = FakeClient([]), FakeClient([])
    pool = FakePool([free, busy], busy={"sessions/w1.jsession"})
    run_poll(pool)

    assert not free.connected and free.session._entities == set()
    assert busy.connected  # its job disconnects it


def test_client_replaced_during_the_round_is_not_polled():
    # new proxies rebuild the clients mid-round: the round's old one has no path any more, and a
    # reply saved under "None:<id>" would get the person answered again under the real path
    stale = FakeClient([dialog(user(5))])
    pool = FakePool([stale])
    pool._paths[id(stale)] = None
    replied, notes = run_poll(pool)

    assert not stale.connected and stale.sent == [] and replied == {} and notes == []


def test_polled_worker_is_busy_only_during_its_poll():
    seen = []

    class Watched(FakeClient):
        async def iter_dialogs(self, limit):
            seen.append(pool.busy("sessions/w0.jsession"))
            return
            yield

    client = Watched([])
    pool = FakePool([client])
    run_poll(pool)

    assert seen == [True]  # a scrape can't start on it meanwhile
    assert pool.polling is None and not client.connected  # its own flag didn't keep it connected


def test_take_slot_refuses_the_worker_autoreply_polls():
    from bot.routers import scraping
    from bot.services.delegation import WorkerPool
    from bot.services.jobs import JobManager

    pool = WorkerPool(pytypes.SimpleNamespace(sessions=[], get_session_path=lambda c: None))
    pool.polling = "sessions/a.jsession"
    told = []

    async def menu(text):
        told.append(text)

    account = pytypes.SimpleNamespace(personal=False, path="sessions/a.jsession")
    progress = asyncio.run(scraping._take_slot(JobManager(), pool, account, "Скрап", menu, menu))

    assert progress is None and told == [scraping.WORKER_BUSY] and pool.scraping is None


def test_undelivered_notice_leaves_the_chat_unread():
    client = FakeClient([dialog(user(5), msg_id=42)])
    replied = {}

    async def bot_api_down(text, url):
        return False

    asyncio.run(autoreply.poll_once(FakePool([client]), TEXT, replied, bot_api_down))

    assert client.sent == [(5, TEXT)] and list(replied)  # answered once and remembered
    assert client.read == []  # the next round forwards it, without another reply


def test_notify_reports_whether_any_admin_got_it():
    class Bot:
        def __init__(self, fail):
            self.fail = fail

        async def send_message(self, chat_id, text, reply_markup=None):
            if self.fail:
                raise RuntimeError("network down")

    assert asyncio.run(autoreply.make_notify(Bot(False), {1})("hi", "https://t.me/ann")) is True
    assert asyncio.run(autoreply.make_notify(Bot(True), {1, 2})("hi", "https://t.me/ann")) is False


def test_hanging_connect_does_not_stall_the_round(monkeypatch):
    monkeypatch.setattr(autoreply, "WORKER_TIMEOUT", 0.05)

    class DeadProxy(FakeClient):
        async def connect(self):
            await asyncio.sleep(3600)

    dead, ok = DeadProxy([dialog(user(4))]), FakeClient([dialog(user(5))])
    run_poll(FakePool([dead, ok]))

    assert dead.sent == [] and ok.sent == [(5, TEXT)]


def test_scraping_worker_untouched():
    client = FakeClient([dialog(user(5))])
    run_poll(FakePool([client], scraping="sessions/w0.jsession"))
    assert client.sent == [] and not client.connected


def test_one_failing_worker_does_not_stop_the_round():
    broken = FakeClient([], fail=ConnectionError("dropped"))
    ok = FakeClient([dialog(user(5))])
    run_poll(FakePool([broken, ok]))

    assert ok.sent == [(5, TEXT)]
    assert not broken.connected


@pytest.mark.parametrize("enabled, text, polled", [
    (True, TEXT, True),
    (False, TEXT, False),
    (True, "", False),
])
def test_loop_polls_only_while_on(monkeypatch, enabled, text, polled):
    rounds = []

    async def fake_poll(pool, reply_text, replied, notify):
        rounds.append(reply_text)

    async def fake_sleep(seconds):
        if len(sleeps) == 2:
            raise asyncio.CancelledError
        sleeps.append(seconds)

    sleeps = []
    monkeypatch.setattr(autoreply, "poll_once", fake_poll)
    monkeypatch.setattr(autoreply.asyncio, "sleep", fake_sleep)
    settings = pytypes.SimpleNamespace(autoreply_enabled=enabled, autoreply_text=text, autoreply_interval=900)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(autoreply._loop(FakeBot(), FakePool([]), settings, {1}))
    assert sleeps == [900, 900]
    assert rounds == ([TEXT, TEXT] if polled else [])


# --- config.toml writes and the bot screen -------------------------------------------

import toml  # noqa: E402

from bot.callbacks import MenuAction, MenuCB  # noqa: E402
from bot.keyboards.menu import workers_kb  # noqa: E402
from bot.routers import autoreply as screen  # noqa: E402


CONFIG = (
    "[bot]\n"
    "admins = [1]  # keep me\n\n"
    "[broadcast]\n"
    "messages = []\n"
    "delay = [5, 10]\n"
    "messages_count = 0\n"
    'trigger = ""\n'
)


def settings_with(tmp_path, monkeypatch, config=CONFIG):
    from modules.settings import Settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", "h")
    (tmp_path / "config.toml").write_text(config, encoding="utf-8")
    return Settings()


def test_settings_default_on_but_textless(tmp_path, monkeypatch):
    settings = settings_with(tmp_path, monkeypatch)
    assert settings.autoreply_enabled and settings.autoreply_text == "" and settings.autoreply_interval == 900


def test_set_text_adds_the_missing_section_and_keeps_the_rest(tmp_path, monkeypatch):
    settings = settings_with(tmp_path, monkeypatch)
    tricky = 'Спасибо! "Пишите" @MainMask \\ 🙂\nвторая строка\tтаб'
    settings.set_autoreply_text(tricky)

    text = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert toml.loads(text)["autoreply"]["text"] == tricky and settings.autoreply_text == tricky
    assert "admins = [1]  # keep me\n" in text  # only appended: comments survive
    assert text.startswith(CONFIG)


def test_set_text_and_toggle_rewrite_only_their_lines(tmp_path, monkeypatch):
    settings = settings_with(tmp_path, monkeypatch, CONFIG + (
        "\n[autoreply]\n"
        "enabled = true  # on/off\n"
        'text = "old \\"quoted\\""  # the reply\n'
        "interval = 900\n"
    ))
    settings.set_autoreply_text("new")
    settings.set_autoreply_enabled(False)

    text = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert 'text = "new"  # the reply\n' in text and "enabled = false  # on/off\n" in text
    assert "interval = 900\n" in text and not settings.autoreply_enabled


def test_set_enabled_adds_the_key_under_an_existing_section(tmp_path, monkeypatch):
    settings = settings_with(tmp_path, monkeypatch, CONFIG + '\n[autoreply]\ntext = "hi"\n')
    settings.set_autoreply_enabled(False)
    assert toml.load(tmp_path / "config.toml")["autoreply"] == {"enabled": False, "text": "hi"}


def test_set_delay_still_rewrites_its_line(tmp_path, monkeypatch):
    settings = settings_with(tmp_path, monkeypatch)
    settings.set_delay([2, 4])
    text = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert "delay = [2, 4]\n" in text and "# keep me" in text


class _State:
    def __init__(self):
        self.state = None

    async def set_state(self, state):
        self.state = state

    async def clear(self):
        self.state = None


class _Msg:
    def __init__(self, text=None):
        self.text = text
        self.answers, self.edits = [], []

    async def answer(self, text, **kwargs):
        self.answers.append(text)

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs.get("reply_markup")))


class _Callback:
    def __init__(self):
        self.message = _Msg()
        self.alerts = []

    async def answer(self, text=None, **kwargs):
        self.alerts.append(text)


def _pool():
    return pytypes.SimpleNamespace(storage=pytypes.SimpleNamespace(usernames={"sessions/a.jsession": "worker_a"}))


def test_workers_screen_has_the_autoreply_button():
    actions = [MenuCB.unpack(b.callback_data).action for row in workers_kb().inline_keyboard
               for b in row if b.callback_data.startswith("menu:")]
    assert MenuAction.AUTOREPLY in actions


def test_screen_shows_status_text_and_stats(tmp_path, monkeypatch):
    settings = settings_with(tmp_path, monkeypatch)
    settings.set_autoreply_text("пишите <@MainMask>")
    autoreply.save_replied({"sessions/a.jsession:1": datetime.now().strftime(autoreply.TIME_FORMAT),
                            "sessions/b.jsession:2": "2020-01-01 00:00:00"})
    callback = _Callback()
    asyncio.run(screen.autoreply_screen(callback, _State(), settings, _pool()))

    text, markup = callback.message.edits[0]
    assert "✅ включён" in text and "раз в 15 мин" in text
    assert "пишите &lt;@MainMask&gt;" in text
    assert "всего 2 · за сутки 1 · за 7 дней 1" in text
    assert "@worker_a — 1" in text and "b.jsession — 1" in text
    assert markup.inline_keyboard[0][0].text == "⏸ Выключить"


def test_toggle_off_and_on(tmp_path, monkeypatch):
    settings = settings_with(tmp_path, monkeypatch)
    settings.set_autoreply_text("hi")
    callback = _Callback()

    asyncio.run(screen.autoreply_toggle(callback, settings, _pool()))
    assert not settings.autoreply_enabled and "⏸ выключен" in callback.message.edits[-1][0]
    assert toml.load(tmp_path / "config.toml")["autoreply"]["enabled"] is False

    asyncio.run(screen.autoreply_toggle(callback, settings, _pool()))
    assert settings.autoreply_enabled


def test_cannot_turn_on_without_text(tmp_path, monkeypatch):
    settings = settings_with(tmp_path, monkeypatch)
    settings.set_autoreply_enabled(False)
    callback = _Callback()

    asyncio.run(screen.autoreply_toggle(callback, settings, _pool()))
    assert not settings.autoreply_enabled and "Сначала задайте текст" in callback.alerts[0]
    assert callback.message.edits == []


def test_new_text_is_saved(tmp_path, monkeypatch):
    settings = settings_with(tmp_path, monkeypatch)
    state, callback = _State(), _Callback()
    asyncio.run(screen.autoreply_text_start(callback, state))
    assert state.state == screen.SetAutoreplyText.input

    empty = _Msg(text=None)  # a sticker / photo
    asyncio.run(screen.autoreply_text_apply(empty, state, settings))
    assert settings.autoreply_text == "" and state.state is not None

    msg = _Msg(text="  Пишите @MainMask  ")
    asyncio.run(screen.autoreply_text_apply(msg, state, settings))
    assert settings.autoreply_text == "Пишите @MainMask" and state.state is None
    assert toml.load(tmp_path / "config.toml")["autoreply"]["text"] == "Пишите @MainMask"
    assert "сохранён" in msg.answers[0]
