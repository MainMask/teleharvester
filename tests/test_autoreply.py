"""Offline tests for the auto-reply to PM-mailing replies (bot/services/autoreply.py)."""

import asyncio
import conftest
import json
import os
import types as pytypes
from datetime import datetime, timezone

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from telethon import types

from bot.services import autoreply


TEXT = "Для связи пишите @MainMask"
real_sleep = asyncio.sleep  # the loop's tests replace asyncio.sleep: theirs still lets the started rounds run


@pytest.fixture(autouse=True)
def _isolated_replied(monkeypatch, tmp_path):
    monkeypatch.setattr(autoreply, "REPLIED_PATH", str(tmp_path / "auto_replies.json"))
    monkeypatch.setattr(autoreply, "_not_mailing", set())


def user(user_id=1, **kw):
    fields = dict(id=user_id, first_name="Ann", last_name=None, username="ann",
                  bot=False, is_self=False, deleted=False, phone=None)
    fields.update(kw)
    return types.User(**fields)


WRITTEN = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)  # when a fake message was written


def message(text="hi", out=False, msg_id=10, date=WRITTEN):
    return pytypes.SimpleNamespace(out=out, message=text, id=msg_id, date=date)


def dialog(entity, unread=1, out=False, text="hi", msg_id=10, history=None):
    """history: the chat's messages, newest first (default: just the last one)."""
    last = message(text, out, msg_id)
    item = pytypes.SimpleNamespace(entity=entity, unread_count=unread, message=last)
    item.history = history if history is not None else [last]
    return item


class FakeClient:
    def __init__(self, dialogs, wrote_first=True, fail=None, last_mine="рассылка"):
        self.dialogs = dialogs
        self.last_mine = last_mine  # the worker's own last message in the chat
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
            return [message(self.last_mine, out=True)] if self.wrote_first else []
        return self.by_id[entity.id].history[:limit]

    async def send_message(self, entity, text, parse_mode="md"):
        self.sent.append((entity.id, text))
        self.parse_modes.append(parse_mode)

    async def send_read_acknowledge(self, entity, max_id):
        self.read.append((entity.id, max_id))


class FakePool:
    def __init__(self, clients, busy=(), scraping=None, home=None):
        self.workers = list(clients)
        self._paths = {id(c): f"sessions/w{i}.jsession" for i, c in enumerate(clients)}
        self._busy = set(busy)
        self.polling = None
        self.scraping = pytypes.SimpleNamespace(path=scraping) if scraping else None
        self.storage = pytypes.SimpleNamespace(
            get_session_path=lambda c: self._paths[id(c)],
            usernames={}, directory="sessions", home=home or conftest.home(),
        )

    def busy(self, path):  # as WorkerPool.busy
        return path in self._busy or path == self.polling


def run_poll(pool, replied=None):
    notes = []

    async def notify(text, url, reply=None):
        notes.append((text, url))
        return True

    replied = {} if replied is None else replied
    asyncio.run(autoreply.poll_once(pool, TEXT, replied, notify, 1, pool.workers))
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


def test_reply_sent_but_not_recorded_is_not_sent_again():
    # a round's connection dropped after the reply went out: the person writes again
    client = FakeClient([dialog(user(5), text="а подробнее?", msg_id=11)], last_mine=TEXT + "\n")
    replied, notes = run_poll(FakePool([client]))

    assert client.sent == []
    assert list(replied) == ["sessions/w0.jsession:5"]
    assert client.read == [(5, 11)]
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


def test_a_settings_refusal_is_named_in_the_notice():
    from telethon.errors import ForbiddenError

    client = FakeClient([dialog(user(5))])

    async def send(entity, text, **kwargs):
        raise ForbiddenError(None, "PRIVACY_PREMIUM_REQUIRED")

    client.send_message = send
    replied, notes = run_poll(FakePool([client]))

    assert replied == {} and "НЕ отправлен: пишут только контакты и Premium" in notes[0][0]


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
    assert "Ann (🆔 <code>5</code>), 📱 +79990001122" in text and url == "tg://user?id=5"


class FakeBot:
    def __init__(self, reject_buttons=False, floods=0):
        self.reject_buttons = reject_buttons
        self.floods = floods
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        assert parse_mode == "HTML"  # the id in <code>, everything from Telegram escaped
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
        "projects/2/sessions/c.jsession:4": "2026-10-06 11:00:00",  # another project's worker: not counted
    }
    stats = autoreply.reply_stats(replied, datetime(2026, 10, 6, 12, 0, 0), "sessions")
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
    progress = asyncio.run(scraping.common.take_slot(JobManager(), pool, account, "Скрап", menu))

    assert progress is None and told == [scraping.common.WORKER_BUSY] and pool.scraping is None


def test_undelivered_notice_leaves_the_chat_unread():
    client = FakeClient([dialog(user(5), msg_id=42)])
    replied = {}

    async def bot_api_down(text, url, reply=None):
        return False

    asyncio.run(autoreply.poll_once(FakePool([client]), TEXT, replied, bot_api_down, 1, [client]))

    assert client.sent == [(5, TEXT)] and list(replied)  # answered once and remembered
    assert client.read == []  # the next round forwards it, without another reply


def test_notify_reports_whether_any_admin_got_it():
    class Bot:
        def __init__(self, fail):
            self.fail = fail

        async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
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


def test_worker_released_mid_poll_does_not_end_the_round():
    # a job's release_client() cancels the poll's in-flight request (Telethon cancels pending
    # futures on disconnect): that worker only, not the whole loop
    released = FakeClient([], fail=asyncio.CancelledError())
    ok = FakeClient([dialog(user(5))])
    run_poll(FakePool([released, ok]))

    assert ok.sent == [(5, TEXT)]


def test_bot_stopping_still_cancels_the_round():
    class Hanging(FakeClient):
        async def iter_dialogs(self, limit):
            await asyncio.sleep(3600)
            yield

    async def notify(text, url, reply=None):
        return True

    async def main():
        pool = FakePool([Hanging([])])
        task = asyncio.create_task(autoreply.poll_once(pool, TEXT, {}, notify, 1, pool.workers))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())


@pytest.mark.parametrize("enabled, text, mailed, polled", [
    (True, TEXT, False, [TEXT]),
    (False, TEXT, False, []),
    (True, "", False, []),
    # once the project has mailed, its replies are tracked with the auto-reply off: polled without a text
    (False, TEXT, True, [None]),
    (True, "", True, [None]),
])
def test_loop_polls_while_on_or_once_the_project_has_mailed(monkeypatch, tmp_path, enabled, text, mailed, polled):
    rounds = []

    async def fake_poll(pool, reply_text, replied, notify, project, clients):
        rounds.append(reply_text)

    async def fake_sleep(seconds):
        await real_sleep(0)  # as a real sleep: the rounds started (each a task) get their turn
        if len(sleeps) == 2:
            raise asyncio.CancelledError
        sleeps.append(seconds)

    sleeps = []
    monkeypatch.setattr(autoreply, "poll_once", fake_poll)
    monkeypatch.setattr(autoreply.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(autoreply.random, "uniform", lambda low, high: 0)  # the first check: at once
    settings = pytypes.SimpleNamespace(autoreply_enabled=enabled, autoreply_text=text, autoreply_interval=[600, 1200])
    if mailed:
        (tmp_path / autoreply.MAILING_STATS).write_text("{}")

    pool = FakePool([FakeClient([])], home=str(tmp_path))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(autoreply._loop(FakeBot(), conftest.Workspaces(pool), settings, {1}))
    # the next check 10–20 min away: the loop looks again within a TICK
    assert sleeps == [autoreply.TICK, autoreply.TICK]
    assert rounds == polled[:1]


def test_the_loop_answers_with_each_project_s_own_text_and_skips_the_ones_off(monkeypatch, tmp_path):
    rounds = []

    async def fake_poll(pool, reply_text, replied, notify, project, clients):
        rounds.append((pool.storage.home, reply_text))

    async def fake_sleep(seconds):
        await real_sleep(0)  # as a real sleep: the rounds started (each a task) get their turn
        if sleeps:
            raise asyncio.CancelledError
        sleeps.append(seconds)

    sleeps = []
    monkeypatch.setattr(autoreply, "poll_once", fake_poll)
    monkeypatch.setattr(autoreply.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(autoreply.random, "uniform", lambda low, high: 0)
    own, off, default = (str(tmp_path / name) for name in ("own", "off", "default"))
    autoreply.save_project_settings(own, True, "свой текст")
    autoreply.save_project_settings(off, False, "выключен")
    pools = [FakePool([FakeClient([])], home=home) for home in (own, off, default)]
    for number, pool in enumerate(pools):  # each project's own sessions/
        pool._paths = {key: f"{number}/{path}" for key, path in pool._paths.items()}
    workspaces = pytypes.SimpleNamespace(all=lambda: [pytypes.SimpleNamespace(pool=pool, project_id=1)
                                                      for pool in pools])
    settings = pytypes.SimpleNamespace(autoreply_enabled=True, autoreply_text=TEXT, autoreply_interval=[600, 1200])
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(autoreply._loop(FakeBot(), workspaces, settings, {1}))
    # a project without its own: config.toml's
    assert rounds == [(own, "свой текст"), (default, TEXT)]


def test_a_project_s_slow_round_holds_up_no_other_project(monkeypatch, tmp_path):
    """Projects side by side: one's dead proxies (WORKER_TIMEOUT a worker) must not delay another's replies."""
    rounds = []

    async def fake_poll(pool, reply_text, replied, notify, project, clients):
        rounds.append(f"{project} start")
        if project == 1:  # its workers slow: their round still under way at the loop's next look
            await real_sleep(0.05)
        rounds.append(f"{project} end")

    async def fake_sleep(seconds):
        await real_sleep(0)  # the rounds started get their turn
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise asyncio.CancelledError

    sleeps = []
    monkeypatch.setattr(autoreply, "poll_once", fake_poll)
    monkeypatch.setattr(autoreply.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(autoreply.random, "uniform", lambda low, high: 0)
    pools = [FakePool([FakeClient([])], home=str(tmp_path / str(number))) for number in (1, 2)]
    for number, pool in enumerate(pools):
        pool._paths = {key: f"{number}/{path}" for key, path in pool._paths.items()}
    workspaces = pytypes.SimpleNamespace(all=lambda: [pytypes.SimpleNamespace(pool=pool, project_id=number)
                                                      for number, pool in enumerate(pools, 1)])
    settings = pytypes.SimpleNamespace(autoreply_enabled=True, autoreply_text=TEXT, autoreply_interval=[600, 1200])
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(autoreply._loop(FakeBot(), workspaces, settings, {1}))
    # 2's round while 1's goes on; 1's polled once: its round under way, the loop doesn't start another
    assert rounds[:3] == ["1 start", "2 start", "2 end"] and rounds.count("1 start") == 1


# --- config.toml writes and the bot screen -------------------------------------------

import toml  # noqa: E402

from bot.callbacks import AutoreplyCB, NavCB  # noqa: E402
from bot.states import origin  # noqa: E402
from bot.keyboards.menu import screen_kb  # noqa: E402
from modules.registry import AUTOREPLY, BASE, SETTINGS  # noqa: E402
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
    assert settings.autoreply_enabled and settings.autoreply_text == "" and settings.autoreply_interval == [600, 1200]


def test_a_setting_adds_its_missing_section_and_keeps_the_rest(tmp_path, monkeypatch):
    settings = settings_with(tmp_path, monkeypatch)
    settings.set_daily_cap("per_account_daily", 30)

    text = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert toml.loads(text)["limits"]["per_account_daily"] == 30 and settings.per_account_daily == 30
    assert "admins = [1]  # keep me\n" in text  # only appended: comments survive
    assert text.startswith(CONFIG)


def test_a_tricky_text_rewrites_only_its_line(tmp_path, monkeypatch):
    settings = settings_with(tmp_path, monkeypatch, CONFIG.replace('trigger = ""\n', 'trigger = "old"  # the word\n'))
    tricky = 'Спасибо! "Пишите" @MainMask \\ 🙂\nвторая строка\tтаб'
    settings.set_trigger(tricky)

    text = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert toml.loads(text)["broadcast"]["trigger"] == tricky and settings.trigger == tricky
    assert "  # the word\n" in text and "delay = [5, 10]\n" in text


def test_a_setting_adds_its_key_under_an_existing_section(tmp_path, monkeypatch):
    settings = settings_with(tmp_path, monkeypatch, CONFIG + "\n[limits]\nprofile_pause = [60, 180]\n")
    settings.set_daily_cap("invite_per_account_daily", 5)
    assert toml.load(tmp_path / "config.toml")["limits"] == {"profile_pause": [60, 180],
                                                              "invite_per_account_daily": 5}


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
        self.text, self.chat = text, pytypes.SimpleNamespace(id=1)
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


def _workspaces():
    """Every project's workers, as the screen names them (Workspaces.username)."""
    return pytypes.SimpleNamespace(username={"sessions/a.jsession": "worker_a"}.get)


PROJECT = pytypes.SimpleNamespace(project_id=3)


def _own(home=None) -> dict:
    """What the project's autoreply.json holds ({} without one)."""
    path = os.path.join(home or conftest.home(), autoreply.SETTINGS_NAME)
    return json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}


def _screen(settings, pool=None):
    callback = _Callback()
    asyncio.run(screen.autoreply_screen(callback.message, _State(), True, settings, pool or FakePool([]), PROJECT,
                                        _workspaces()))
    return callback.message.edits[0]


def test_the_autoreply_is_a_screen_of_the_project_s_base_and_leads_back_there(tmp_path, monkeypatch):
    from bot.routers import menu, settings as settings_screen  # noqa: F401 — its view registered

    # a screen of 🎯 База и рассылка (its ✖️ of the text comes back to it: bot/states.origin), not of ⚙️ Настройки
    opened = [NavCB.unpack(b.callback_data).screen for row in screen_kb(BASE, 3).inline_keyboard
              for b in row if b.callback_data.startswith("nav:")]
    assert AUTOREPLY in opened and origin("SetAutoreplyText:input", {}) == AUTOREPLY
    assert AUTOREPLY not in [NavCB.unpack(b.callback_data).screen for row in screen_kb(SETTINGS).inline_keyboard
                             for b in row if b.callback_data.startswith("nav:")]
    settings = settings_with(tmp_path, monkeypatch)
    text, markup = _screen(settings)
    back = markup.inline_keyboard[-1][-1]
    assert back.text == "⬅️ Назад" and NavCB.unpack(back.callback_data) == NavCB(screen=BASE, project=3)
    # its buttons act on their project, whichever the chat is in by then
    assert {(NavCB if b.callback_data.startswith("nav:") else AutoreplyCB).unpack(b.callback_data).project
            for row in markup.inline_keyboard for b in row} == {3}
    # its stats grow as people answer: 🔄 shows the screen anew, in its place
    refresh = next(b for row in markup.inline_keyboard for b in row if b.text == "🔄 Обновить")
    assert NavCB.unpack(refresh.callback_data) == NavCB(screen=AUTOREPLY, project=3)

    callback = _Callback()
    asyncio.run(menu.open_by_button(callback, NavCB(screen=SETTINGS), _State(), settings=settings))
    assert "автоответ" not in callback.message.edits[0][0]  # each project's own: not a ⚙️ setting


def test_a_very_long_text_still_fits_the_screen(tmp_path, monkeypatch):
    settings = settings_with(tmp_path, monkeypatch)
    autoreply.save_project_settings(conftest.home(), True, "&" * 5000)  # the file may be edited by hand
    text, _ = _screen(settings)
    assert len(text) <= 4096 and "&amp;…</blockquote>" in text  # escaped after it is cut


def test_a_long_save_error_fits_the_alert(tmp_path, monkeypatch):
    settings = settings_with(tmp_path, monkeypatch, CONFIG + '\n[autoreply]\ntext = "hi"\n')

    def broken(home, enabled, text):
        raise OSError("x" * 500)

    monkeypatch.setattr(autoreply, "save_project_settings", broken)
    callback = _Callback()
    asyncio.run(screen.autoreply_toggle(callback, settings, FakePool([]), PROJECT, _workspaces()))
    assert len(callback.alerts[0]) <= 200  # Telegram refuses a longer one: the tap would get no answer


def test_screen_shows_status_text_and_the_project_s_stats(tmp_path, monkeypatch):
    settings = settings_with(tmp_path, monkeypatch, CONFIG + '\n[autoreply]\ntext = "пишите <@MainMask>"\n')
    now = datetime.now().strftime(autoreply.TIME_FORMAT)
    autoreply.save_replied({"sessions/a.jsession:1": now, "sessions/b.jsession:2": "2020-01-01 00:00:00",
                            "projects/9/sessions/c.jsession:3": now})  # another project's
    text, markup = _screen(settings)
    assert "<b>📬 Автоответ</b> — ✅ включён" in text and "каждого раз в 10–20 мин, в случайный момент" in text
    assert "пишите &lt;@MainMask&gt;" in text  # config.toml's: the project has none of its own
    assert "всего 2 · за сутки 1 · за 7 дней 1" in text
    assert "@worker_a — 1" in text and "b.jsession — 1" in text and "c.jsession" not in text
    assert markup.inline_keyboard[0][0].text == "⏸ Выключить"


def _replied_by(workers: int) -> dict:
    """worker i (sessions/wI.jsession) answered i + 1 people."""
    now = datetime.now().strftime(autoreply.TIME_FORMAT)
    return {f"sessions/w{i}.jsession:{n}": now for i in range(workers) for n in range(i + 1)}


def _buttons(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def test_screen_shows_the_top_workers_and_a_button_for_all(tmp_path, monkeypatch):
    settings = settings_with(tmp_path, monkeypatch)
    autoreply.save_replied(_replied_by(screen.TOP_WORKERS + 3))  # w12 13 answers ... w0 1
    text, markup = _screen(settings)
    shown = [line for line in text.splitlines() if line.startswith("• ")]
    assert len(shown) == screen.TOP_WORKERS and shown[0] == "• w12.jsession — 13"
    assert "…и ещё воркеров: 3, ответов у них: 6" in text  # w2, w1, w0: 3 + 2 + 1
    assert "📋 Все воркеры" in _buttons(markup)


def test_few_workers_no_button(tmp_path, monkeypatch):
    settings = settings_with(tmp_path, monkeypatch)
    autoreply.save_replied(_replied_by(screen.TOP_WORKERS))
    text, markup = _screen(settings)
    assert "и ещё воркеров" not in text and "📋 Все воркеры" not in _buttons(markup)


def test_all_workers_button_sends_every_worker_in_chunks(tmp_path, monkeypatch):
    settings_with(tmp_path, monkeypatch)
    autoreply.save_replied(_replied_by(300))  # ~6 000 characters: more than one message
    callback = _Callback()
    asyncio.run(screen.autoreply_workers(callback, FakePool([]), PROJECT, _workspaces()))

    answers = callback.message.answers
    assert len(answers) > 1 and all(len(chunk) <= 4096 for chunk in answers)
    lines = [line for chunk in answers for line in chunk.splitlines() if line.startswith("• ")]
    assert len(lines) == 300 and lines[0] == "• w299.jsession — 300"


def test_toggle_off_and_on_in_the_project_only(tmp_path, monkeypatch):
    settings = settings_with(tmp_path, monkeypatch, CONFIG + '\n[autoreply]\ntext = "hi"\n')
    config = (tmp_path / "config.toml").read_text(encoding="utf-8")
    callback = _Callback()

    asyncio.run(screen.autoreply_toggle(callback, settings, FakePool([]), PROJECT, _workspaces()))
    assert _own() == {"enabled": False, "text": "hi"} and "⭕ выключен" in callback.message.edits[-1][0]
    # config.toml: the default of the other projects, untouched
    assert (tmp_path / "config.toml").read_text(encoding="utf-8") == config
    assert autoreply.project_settings(str(tmp_path / "other"), settings) == {"enabled": True, "text": "hi"}

    asyncio.run(screen.autoreply_toggle(callback, settings, FakePool([]), PROJECT, _workspaces()))
    assert _own()["enabled"] is True


def test_cannot_turn_on_without_text(tmp_path, monkeypatch):
    settings = settings_with(tmp_path, monkeypatch, CONFIG + "\n[autoreply]\nenabled = false\n")
    callback = _Callback()

    asyncio.run(screen.autoreply_toggle(callback, settings, FakePool([]), PROJECT, _workspaces()))
    assert _own() == {} and "Сначала задайте текст" in callback.alerts[0]
    assert callback.message.edits == []


def test_new_text_is_saved_in_the_project(tmp_path, monkeypatch):
    settings = settings_with(tmp_path, monkeypatch)
    state, callback = _State(), _Callback()
    asyncio.run(screen.autoreply_text_start(callback, state))
    assert state.state == screen.SetAutoreplyText.input

    empty = _Msg(text=None)  # a sticker / photo
    asyncio.run(screen.autoreply_text_apply(empty, state, settings, FakePool([]), PROJECT, _workspaces()))
    assert _own() == {} and state.state is not None

    msg = _Msg(text="  Пишите @MainMask  ")
    asyncio.run(screen.autoreply_text_apply(msg, state, settings, FakePool([]), PROJECT, _workspaces()))
    assert _own() == {"enabled": True, "text": "Пишите @MainMask"} and state.state is None
    assert settings.autoreply_text == ""  # config.toml's default unchanged
    # the screen again under the confirmation, with the new text
    assert msg.answers[0].startswith("✅ Текст автоответа сохранён") and "<b>📬 Автоответ</b>" in msg.answers[0]
    assert "<blockquote>Пишите @MainMask</blockquote>" in msg.answers[0]


def test_the_notification_escapes_what_came_from_telegram_and_fits_after_escaping():
    user = pytypes.SimpleNamespace(first_name="<Ann>", last_name=None, username=None, id=5, phone=None)
    assert autoreply._describe(user) == "&lt;Ann&gt; (🆔 <code>5</code>)"  # a tap copies the id
    text = autoreply._escaped("<b>" + "&" * 5000, autoreply.NOTIFY_TEXT_LIMIT)
    assert text.startswith("&lt;b&gt;&amp;") and text.endswith("&amp;")  # never cut inside an entity
    assert len(text) <= autoreply.NOTIFY_TEXT_LIMIT


def test_a_moved_worker_s_replies_move_in_the_running_loop_s_ledger_too(monkeypatch):
    """The loop saves its ledger whole each round: a rename of the file alone would be undone by it."""
    ledger = {"projects/1/sessions/a.jsession:5": "2026-01-01 10:00:00"}
    monkeypatch.setattr(autoreply, "_replied", ledger)
    autoreply.rename_worker("projects/1/sessions/a.jsession", "projects/2/sessions/a.jsession")
    assert ledger == {"projects/2/sessions/a.jsession:5": "2026-01-01 10:00:00"} == autoreply.load_replied()


# --- replies tracked with the auto-reply off; ✍️ Ответить ---------------------------

def test_with_the_auto_reply_off_a_reply_is_recorded_logged_and_forwarded_unanswered(tmp_path):
    from bot.callbacks import ReplyCB
    from modules import outreach

    client = FakeClient([dialog(user(5), text="интересно, расскажите", msg_id=42)])
    notes, replied = [], {}

    async def notify(text, url, reply=None):
        notes.append((text, reply))
        return True

    asyncio.run(autoreply.poll_once(FakePool([client], home=str(tmp_path)), None, replied, notify, 4, [client]))
    assert client.sent == [] and "sessions/w0.jsession:5" in replied and client.read == [(5, 42)]
    (text, reply), = notes
    assert text.startswith("💬 Ответ на рассылку\n") and "автоответ" not in text
    assert reply == ReplyCB(project=4, worker="w0.jsession", user=5)
    logged = [json.loads(line) for line in (tmp_path / outreach.DIALOGS_NAME).read_text().splitlines()]
    assert [(e["worker"], e["user"], e["text"]) for e in logged] == [("w0.jsession", 5, "интересно, расскажите")]


def test_a_later_message_is_forwarded_and_logged_as_the_dialog_s(tmp_path):
    from modules import outreach

    client = FakeClient([dialog(user(5), text="ещё вопрос")])
    replied = {"sessions/w0.jsession:5": "2026-01-01 10:00:00"}
    _, notes = run_poll(FakePool([client], home=str(tmp_path)), replied)
    assert client.sent == [] and notes[0][0].startswith("💬 Новое сообщение\n")
    logged, = [json.loads(line) for line in (tmp_path / outreach.DIALOGS_NAME).read_text().splitlines()]
    assert logged["text"] == "ещё вопрос" and "dir" not in logged


def test_the_auto_reply_is_logged_after_what_the_person_wrote(tmp_path):
    from modules import outreach

    client = FakeClient([dialog(user(5), text="а что за предложение?")])
    run_poll(FakePool([client], home=str(tmp_path)))
    logged = [json.loads(line) for line in (tmp_path / outreach.DIALOGS_NAME).read_text().splitlines()]
    assert [(e["text"], e.get("dir"), e.get("auto")) for e in logged] == [
        ("а что за предложение?", None, None), (TEXT, "out", True)]


def test_a_reply_is_logged_at_the_time_it_was_written_not_the_round_s(tmp_path):
    # the first message after the worker's last one: older unread ones came before the mailing
    from modules import outreach

    before, first, later = (datetime(2026, 1, 1, hour, tzinfo=timezone.utc) for hour in (8, 10, 11))
    history = [message("ещё", msg_id=13, date=later), message("ответ", msg_id=12, date=first),
               message("рассылка", out=True, msg_id=11, date=first), message("раньше", msg_id=10, date=before)]
    client = FakeClient([dialog(user(5), unread=4, msg_id=13, history=history)])
    run_poll(FakePool([client], home=str(tmp_path)))
    logged = json.loads((tmp_path / outreach.DIALOGS_NAME).read_text().splitlines()[0])
    assert logged["text"] == "раньше\nответ\nещё"
    assert logged["at"] == first.astimezone().strftime(outreach.TIME_FORMAT)


def test_notify_puts_the_answer_button_under_the_profile_one():
    from bot.callbacks import ReplyCB

    bot = FakeBot()
    asyncio.run(autoreply.make_notify(bot, {1})("hi", "https://t.me/ann", ReplyCB(project=2, worker="w.jsession",
                                                                                   user=5)))
    (_, _, markup), = bot.sent
    answer = markup.inline_keyboard[1][0]
    assert answer.text == "✍️ Ответить" and ReplyCB.unpack(answer.callback_data).user == 5


def test_a_profile_link_privacy_forbids_keeps_the_answer_button():
    class Bot(FakeBot):
        async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
            if reply_markup is not None and any(b.url for row in reply_markup.inline_keyboard for b in row):
                raise TelegramBadRequest(method=None, message="BUTTON_USER_PRIVACY_RESTRICTED")
            self.sent.append((chat_id, text, reply_markup))

    from bot.callbacks import ReplyCB

    bot = Bot()
    asyncio.run(autoreply.make_notify(bot, {1})("hi", "tg://user?id=5", ReplyCB(project=2, worker="w", user=5)))
    (_, _, markup), = bot.sent
    assert [b.text for row in markup.inline_keyboard for b in row] == ["✍️ Ответить"]


# --- each worker on a random timer of its own ---------------------------------------

def test_each_worker_is_checked_at_random_10_to_20_minutes_apart_never_in_a_pack(monkeypatch):
    import random
    random.seed(7)
    pool = FakePool([FakeClient([]) for _ in range(5)])
    due, checks = {}, {}
    for second in range(0, 6 * 3600, 5):  # six hours, the loop looking every 5 s
        for client in autoreply.due_workers(pool, due, second, [600, 1200]):
            checks.setdefault(pool.storage.get_session_path(client), []).append(second)
    assert len(checks) == 5
    gaps = [b - a for times in checks.values() for a, b in zip(times, times[1:])]
    assert min(gaps) >= 600 and max(gaps) <= 1205  # within the range, but for the loop's own step
    assert len(set(gaps)) > len(gaps) // 2  # no fixed period
    firsts = sorted(times[0] for times in checks.values())
    assert firsts[-1] - firsts[0] > 300 and len(set(firsts)) == 5  # not one after another at start


def test_a_worker_gone_or_its_project_off_gets_a_new_first_check_when_back(monkeypatch):
    rounds = []

    async def fake_poll(pool, reply_text, replied, notify, project, clients):
        rounds.append(len(clients))

    async def fake_sleep(seconds):
        await real_sleep(0)  # as a real sleep: the rounds started (each a task) get their turn
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise asyncio.CancelledError

    sleeps, draws = [], iter([0, 30])
    monkeypatch.setattr(autoreply, "poll_once", fake_poll)
    monkeypatch.setattr(autoreply.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(autoreply.random, "uniform", lambda low, high: next(draws))
    settings = pytypes.SimpleNamespace(autoreply_enabled=True, autoreply_text=TEXT, autoreply_interval=[600, 1200])
    pool = FakePool([FakeClient([]), FakeClient([])])
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(autoreply._loop(FakeBot(), conftest.Workspaces(pool), settings, {1}))
    # the first worker at once, the second 30 s later: the loop sleeps until it, not a whole TICK
    assert rounds == [1] and 28 <= sleeps[0] <= 30


def test_a_worker_whose_file_name_is_too_long_for_the_answer_button_is_still_forwarded():
    # a hand-placed .session may have any name: the button's callback data holds at most 64 bytes
    from bot.callbacks import ReplyCB

    bot = FakeBot()
    reply = ReplyCB(project=2, worker="x" * 60 + ".session", user=5)
    assert asyncio.run(autoreply.make_notify(bot, {1})("hi", "https://t.me/ann", reply)) is True
    (_, _, markup), = bot.sent
    assert [b.text for row in markup.inline_keyboard for b in row] == ["👤 Открыть профиль"]
