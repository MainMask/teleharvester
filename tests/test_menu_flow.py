"""The menu's ways back: a job's report leads to its screen in its project, a form's ✖️ to where it was
started; and the buttons that stand for a typed «-» (bot/keyboards/menu.back_kb, bot/states.origin)."""

import asyncio
import types

import pytest
import toml

import conftest
from bot.callbacks import ChoiceCB, MenuAction, MenuCB, NavCB
from bot.keyboards.menu import back_kb
from modules import projects
from modules.registry import (ACCOUNT_LIST, ACCOUNTS, ACTIVITY, BASE, BOT_FUNCTIONS_BY_KEY, CARD, COLLECT, MANAGE,
                              PROFILE, PROJECTS, SECURITY, SETTINGS)
from modules.settings import count_text


def ns(**kw):
    return types.SimpleNamespace(**kw)


class _State:
    def __init__(self, data=None, state=None):
        self.data, self.state = dict(data or {}), state

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kw):
        self.data.update(kw)

    async def set_state(self, state):
        self.state = state

    async def get_state(self):
        return getattr(self.state, "state", self.state)

    async def clear(self):
        self.data, self.state = {}, None


class _Msg:
    def __init__(self, text=None):
        self.text, self.chat, self.answers, self.edits, self.markups = text, ns(id=1), [], [], []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs.get("reply_markup")))

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs.get("reply_markup")))

    async def edit_reply_markup(self, reply_markup=None):
        self.markups.append(reply_markup)


class _Callback:
    def __init__(self):
        self.message, self.alerts = _Msg(), []

    async def answer(self, text=None, show_alert=False):
        if text:
            self.alerts.append(text)


def _nav(markup) -> NavCB:
    [[button]] = markup.inline_keyboard
    return NavCB.unpack(button.callback_data)


# --- a job's report: ⬅️ to the screen its function is on, in its project ---

def test_a_function_is_run_from_its_screen():
    screen = {key: function.category for key, function in BOT_FUNCTIONS_BY_KEY.items()}
    assert screen["reactions"] == ACTIVITY and screen["2fa"] == SECURITY and screen["name"] == PROFILE
    assert screen["status"] == ACCOUNT_LIST
    assert screen["pmmailing"] == BASE and screen["members"] == COLLECT  # the card's screens on its files


def test_a_job_s_report_leads_back_to_its_screen_in_its_project(monkeypatch):
    from bot.services import jobs
    from bot.services.workspaces import Workspace
    from modules.registry import BOT_FUNCTIONS_BY_KEY

    finals = []

    class _Reporter:
        def __init__(self, *args, final_markup=None, **kwargs):
            finals.append(final_markup)

        async def start(self):
            return None

        async def finish(self, text):
            return None

    async def pool_run(instance, bot_function, start, report, only=None):
        return None

    monkeypatch.setattr(jobs, "TelegramReporter", _Reporter)
    workspace = Workspace(7, ns(workers=[], run=pool_run), {})  # its slots know their project

    async def scenario():
        await workspace.manager.run(None, 1, workspace.pool, ns(), BOT_FUNCTIONS_BY_KEY["reactions"],
                                    lambda f, r: None, "Реакции…")
        await asyncio.sleep(0.01)

    asyncio.run(scenario())
    [markup] = finals
    assert markup.inline_keyboard[0][0].text == f"⬅️ {ACTIVITY}"
    assert _nav(markup) == NavCB(screen=ACTIVITY, project=7, fresh=True)


def test_back_to_the_card_says_so():
    assert back_kb(CARD, 3).inline_keyboard[0][0].text == "⬅️ К проекту"


def test_a_report_s_back_opens_its_project_s_screen_as_a_new_message(monkeypatch):
    from bot.routers import menu

    green = projects.add("Green", ["@g"])
    workspaces = conftest.Workspaces()
    workspaces.open(1, green)  # its project: ProjectMiddleware made it the chat's (test_workspaces.py)
    callback = _Callback()
    asyncio.run(menu.open_by_button(callback, NavCB(screen=ACTIVITY, project=green.id, fresh=True), _State(),
                                    workspaces=workspaces, workspace=workspaces.workspace))
    assert callback.message.markups == [None]  # the report stays, without its ⬅️
    text, markup = callback.message.answers[0]
    assert text.startswith(f"📁 <b>Green</b> › <b>{ACTIVITY}</b>")  # the screen says whose it is
    # ...and its buttons act on Green, whichever project the chat is in when they are tapped
    assert {NavCB.unpack(markup.inline_keyboard[-1][0].callback_data).project} == {green.id}


# --- a form's ✖️ Отмена: back to where it was started ---

@pytest.mark.parametrize("step, data, screen", [
    ("Reactions:link", {}, ACTIVITY), ("ChatPost:mode", {}, ACTIVITY), ("SetProxy:input", {}, ACCOUNTS),
    ("PickWorkers:marks", {"pick_key": "2fa"}, SECURITY), ("PickWorkers:marks", {"pick_key": "bio"}, PROFILE),
    ("SetSetting:value", {"setting": "delay"}, SETTINGS),
    ("SetSetting:value", {"setting": "cap_pm"}, SETTINGS), ("Scrape:date_min", {}, COLLECT),
    ("Projects:edit", {}, COLLECT),
    ("PmMailing:path", {}, BASE), ("Projects:rename", {}, MANAGE), ("Projects:upload", {}, BASE),
    ("Projects:name", {}, PROJECTS), ("Unknown:step", {}, None),
])
def test_a_form_goes_back_to_where_it_was_started(step, data, screen):
    from bot.states import origin

    assert origin(step, data) == screen


def test_every_form_has_a_way_back():
    """A new StatesGroup is placed in bot/states.ORIGINS, or its ✖️ would lead to the main menu."""
    from aiogram.fsm.state import StatesGroup

    from bot import states

    groups = [name for name, value in vars(states).items()
              if isinstance(value, type) and issubclass(value, StatesGroup) and value is not StatesGroup]
    own = ("PickWorkers", "Projects", "SetSetting")  # by their data: bot/states.origin
    assert [name for name in groups if name not in states.ORIGINS and name not in own] == []


def test_cancel_in_a_project_s_form_goes_back_to_its_screen_in_place():
    from bot.routers import menu
    from bot.states import Reactions

    callback, state = _Callback(), _State(state=Reactions.link)
    asyncio.run(menu.cancel_button(callback, ChoiceCB(scope="cancel", value="Reactions.link"), state,
                                   ns(active=False), conftest.Workspaces().workspace))
    [(text, markup)] = callback.message.edits
    assert text == "Отменено." and state.state is None
    assert _nav(markup) == NavCB(screen=ACTIVITY, project=1, fresh=True)


# --- the buttons that stand for a typed «-» ---

def test_all_recipients_by_button_as_by_a_typed_dash():
    from bot.routers import mailing
    from bot.states import PmMailing

    callback, state = _Callback(), _State({"path": "assets/targets.txt"}, PmMailing.limit)
    asyncio.run(mailing.mail_all(callback, state))
    assert state.data["limit"] is None and state.state == PmMailing.message

    typed = _State({"path": "assets/targets.txt"}, PmMailing.limit)
    asyncio.run(mailing.mail_limit(_Msg("-"), typed))
    assert typed.data == state.data and typed.state == state.state


def test_the_default_list_by_button_as_by_a_typed_dash():
    from bot.routers import mailing

    callback, state = _Callback(), _State({"bases": []})
    asyncio.run(mailing.mail_base(callback, ChoiceCB(scope="mail_base", value="default"), state))
    assert state.data["path"] == mailing.DEFAULT_TARGETS


@pytest.mark.parametrize("key, value", [("keyword", ""), ("max_messages", None)])
def test_no_keyword_and_all_posts_by_button(key, value):
    from bot.routers.scraping import scrape
    from bot.states import Scrape

    callback = _Callback()
    state = _State({"keyword": "war", "max_messages": 50}, Scrape.keyword)
    asyncio.run(scrape.scrape_any(callback, ChoiceCB(scope="scrape_any", value=key), state))
    assert state.data[key] == value and state.state == Scrape.confirm
    assert callback.message.edits[0][0].startswith("🎛 Дополнительно — ")  # in the prompt's place


def test_a_report_without_a_comment_by_button(monkeypatch):
    from bot.routers import moderation
    from bot.states import ReportUser

    runs = []

    async def report_user(message, state, pool, functions, manager, comment):
        runs.append(comment)

    monkeypatch.setattr(moderation, "_report_user", report_user)
    callback = _Callback()
    asyncio.run(moderation.no_comment(callback, _State(state=ReportUser.comment), None, {},
                                      types.SimpleNamespace(active=False)))
    assert runs == [""] and callback.message.markups == [None]  # pressed once: one report


def test_a_tdata_import_without_a_password_by_button(monkeypatch):
    from bot.routers.accounts import upload

    passwords = []

    async def answered(message, state, pool, manager, password):
        passwords.append(password)

    monkeypatch.setattr(upload, "_answered", answered)
    asyncio.run(upload.tdata_no_password(_Callback(), _State(), None, types.SimpleNamespace(active=False)))
    asyncio.run(upload.tdata_password(_Msg("-"), _State(), None, None))  # typed, as before the button
    assert passwords == [None, None]


# --- ⚙️ Настройки: the delays and the daily caps ---

def _settings(tmp_path, monkeypatch):
    from modules.settings import Settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", "h")
    (tmp_path / "config.toml").write_text(toml.dumps({
        "broadcast": {"messages": ["hi"], "messages_count": 0, "trigger": "go", "delay": [5, 10]},
        "limits": {"per_account_daily": 99, "account_pause": [1, 2]},
    }))
    return Settings()


@pytest.mark.parametrize("action, bad, typed, section, key, saved", [
    (MenuAction.CAP_PM, "-1", "40", "limits", "per_account_daily", 40),
    (MenuAction.CAP_INVITE, "x", "0", "limits", "invite_per_account_daily", 0),
    (MenuAction.CAP_CONTACTS, "", "15", "limits", "contacts_per_account_daily", 15),
    (MenuAction.CAP_COMMENTS, "два", "20", "limits", "comments_per_account_daily", 20),
])
def test_a_setting_is_set_from_its_screen(tmp_path, monkeypatch, action, bad, typed, section, key, saved):
    from bot.routers import settings as screen

    settings = _settings(tmp_path, monkeypatch)
    callback, state = _Callback(), _State()
    asyncio.run(screen.setting_start(callback, MenuCB(action=action), state, settings))
    assert "сейчас" in callback.message.answers[0][0]

    retry = _Msg(bad)
    asyncio.run(screen.setting_typed(retry, state, settings))
    assert "Пришлите ещё раз" in retry.answers[0][0] and state.state is not None

    done = _Msg(typed)
    asyncio.run(screen.setting_typed(done, state, settings))
    config = toml.load(tmp_path / "config.toml")
    assert getattr(settings, key) == saved and config[section][key] == saved
    assert config["broadcast"]["delay"] == [5, 10] and config["limits"]["account_pause"] == [1, 2]  # the rest kept
    text, markup = done.answers[0]
    assert text.startswith("✅ ") and state.state is None
    # the settings again, the new value on its button
    shown = f"{screen._BUTTONS[action.value].title}: {count_text(saved) if saved else '♾'}"  # no limit: ♾
    assert shown in [b.text for row in markup.inline_keyboard for b in row] and f"<b>{shown}</b>" in text


@pytest.mark.parametrize("action, applies", [
    (MenuAction.DELAY, "Действует сразу."),  # read before each pause
    (MenuAction.CAP_PM, "Действует для задач, запущенных после этого."),  # a run reads its cap as it starts
    (MenuAction.CAP_COMMENTS, "«🆕 Комментарии — к новым постам» — уже со следующей проверки."),  # read each round
])
def test_a_changed_setting_says_when_it_applies(tmp_path, monkeypatch, action, applies):
    from bot.routers import settings as screen

    settings = _settings(tmp_path, monkeypatch)
    state = _State()
    asyncio.run(screen.setting_start(_Callback(), MenuCB(action=action), state, settings))
    done = _Msg("7")
    asyncio.run(screen.setting_typed(done, state, settings))
    assert applies in done.answers[0][0]
    assert ("Действует сразу" in done.answers[0][0]) == (action == MenuAction.DELAY)


def test_a_cap_is_made_unlimited_by_its_button(tmp_path, monkeypatch):
    from bot.routers import settings as screen
    from bot.states import SetSetting

    settings = _settings(tmp_path, monkeypatch)
    callback, state = _Callback(), _State()
    asyncio.run(screen.setting_start(callback, MenuCB(action=MenuAction.CAP_PM), state, settings))
    labels = [b.text for row in callback.message.answers[0][1].inline_keyboard for b in row]
    assert labels == [screen.UNLIMITED, "✖️ Отмена"]

    unlimited = ChoiceCB.unpack(callback.message.answers[0][1].inline_keyboard[0][0].callback_data)
    tap = _Callback()
    asyncio.run(screen.setting_unlimited(tap, unlimited, state, settings))
    assert settings.per_account_daily == 0 and state.state is None
    assert tap.message.answers[0][0].startswith("✅ ")

    stale = _Callback()  # the step left: the button does nothing
    state.state = None
    asyncio.run(screen.setting_unlimited(stale, unlimited, state, settings))
    assert settings.per_account_daily == 0 and not stale.message.answers
    assert SetSetting.value  # (the step it belongs to)


def test_a_cap_s_unlimited_button_under_another_setting_s_prompt_is_stale(tmp_path, monkeypatch):
    # 📨 ЛС's prompt left unanswered, ⏱ Задержка's asked: the older ♾ must not make the delay 0
    from bot.routers import settings as screen
    from bot.routers._common import STALE_BUTTON

    settings = _settings(tmp_path, monkeypatch)
    cap, delay, state = _Callback(), _Callback(), _State()
    asyncio.run(screen.setting_start(cap, MenuCB(action=MenuAction.CAP_PM), state, settings))
    asyncio.run(screen.setting_start(delay, MenuCB(action=MenuAction.DELAY), state, settings))
    unlimited = ChoiceCB.unpack(cap.message.answers[0][1].inline_keyboard[0][0].callback_data)

    tap = _Callback()
    asyncio.run(screen.setting_unlimited(tap, unlimited, state, settings))
    assert tap.alerts == [STALE_BUTTON] and not tap.message.answers and state.state is not None
    assert settings.delay == [5, 10] and toml.load(tmp_path / "config.toml")["broadcast"]["delay"] == [5, 10]


def test_the_chat_campaign_s_values_are_not_settings_buttons():
    """🔔 Чат — по слову asks for its trigger and messages count itself, and saves them: no second place."""
    actions = {button.action for button in __import__("modules.registry").registry.SCREENS[SETTINGS].buttons}
    assert not actions & {"trigger", "count"}


# --- an edit Telegram refuses (bot/services/safe_edit.py) ---

def _refusing(why):
    """make_request: an edit refused for `why`, anything else sent (and recorded)."""
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import SendMessage

    sent = []

    async def make_request(bot, method):
        if isinstance(method, SendMessage):
            sent.append(method)
            return "sent"
        raise TelegramBadRequest(method=method, message=f"Bad Request: {why}")
    return make_request, sent


def test_the_same_screen_again_is_no_error():
    from aiogram.methods import EditMessageReplyMarkup, EditMessageText

    from bot.services.safe_edit import SafeEdit

    make_request, sent = _refusing("message is not modified: specified new message content and reply markup "
                                   "are exactly the same")
    for method in (EditMessageText(chat_id=1, message_id=2, text="x"), EditMessageReplyMarkup(chat_id=1, message_id=2)):
        assert asyncio.run(SafeEdit()(make_request, None, method)) is True
    assert sent == []


def test_a_message_that_can_t_be_edited_comes_as_a_new_one():
    from aiogram.methods import EditMessageReplyMarkup, EditMessageText

    from bot.services.safe_edit import SafeEdit

    make_request, sent = _refusing("message can't be edited")
    markup = back_kb(ACTIVITY)
    edit = EditMessageText(chat_id=1, message_id=2, text="<b>x</b>", parse_mode="HTML", reply_markup=markup)
    assert asyncio.run(SafeEdit()(make_request, None, edit)) == "sent"
    [new] = sent
    assert (new.chat_id, new.text, new.parse_mode, new.reply_markup) == (1, "<b>x</b>", "HTML", markup)
    # an old message's buttons that can't be dropped: left as they are
    assert asyncio.run(SafeEdit()(make_request, None, EditMessageReplyMarkup(chat_id=1, message_id=2))) is True
    assert len(sent) == 1


def test_another_refusal_is_raised():
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import EditMessageText

    from bot.services.safe_edit import SafeEdit

    make_request, _ = _refusing("chat not found")
    with pytest.raises(TelegramBadRequest):
        asyncio.run(SafeEdit()(make_request, None, EditMessageText(chat_id=1, message_id=2, text="x")))


# --- 🗳 Голосовать: the poll's options as buttons ---

def _poll_pool(busy=False):
    worker = object()
    return ns(workers=[worker], busy=lambda path: busy, scraped=lambda path: False,
              storage=ns(get_session_path=lambda client: "sessions/w.jsession"))


def test_the_poll_s_options_are_offered_as_buttons(monkeypatch):
    from bot.routers import activity
    from bot.states import PollVote

    async def borrow(pool, client, path, work, timeout):
        return await work()

    class _Poll:
        async def answers(self, session, link):
            return ["Да", "Нет", "Не знаю, но очень длинный вариант ответа, который не влезет в кнопку"]

    monkeypatch.setattr(activity, "borrow", borrow)
    msg, state = _Msg("https://t.me/chan/5"), _State()
    asyncio.run(activity.poll_link(msg, state, _poll_pool(), {"PollVoteFunc": _Poll}))
    text, markup = msg.answers[0]
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert state.state == PollVote.option and labels[:2] == ["1. Да", "2. Нет"] and labels[-1] == "✖️ Отмена"
    assert len(labels[2]) <= 43

    runs = []

    class _Manager:
        active = False

        async def run(self, bot, chat_id, pool, instance, bot_function, factory, header):
            runs.append(factory(ns(run=lambda link, option, report: (link, option)), None))

    callback = _Callback()
    callback.message.bot = None
    pick = ChoiceCB.unpack(markup.inline_keyboard[1][0].callback_data)
    asyncio.run(activity.poll_pick(callback, pick, state, _poll_pool(), {"PollVoteFunc": object}, _Manager()))
    assert runs == [("https://t.me/chan/5", 1)] and callback.message.markups == [None]  # «Нет», chosen once


def test_an_older_poll_s_option_is_not_voted_in_another_poll(monkeypatch):
    from bot.routers import activity
    from bot.routers._common import STALE_BUTTON

    async def borrow(pool, client, path, work, timeout):
        return await work()

    class _Poll:
        async def answers(self, session, link):
            return ["Да", "Нет", "Может быть"]

    monkeypatch.setattr(activity, "borrow", borrow)
    old, state = _Msg("https://t.me/chan/5"), _State()
    asyncio.run(activity.poll_link(old, state, _poll_pool(), {"PollVoteFunc": _Poll}))
    stale = ChoiceCB.unpack(old.answers[0][1].inline_keyboard[2][0].callback_data)  # «Может быть» of post 5
    asyncio.run(activity.poll_link(_Msg("https://t.me/chan/9"), state, _poll_pool(), {"PollVoteFunc": _Poll}))

    class _Manager:
        active = False

        async def run(self, *args):
            raise AssertionError("no vote from another poll's button")

    callback = _Callback()
    asyncio.run(activity.poll_pick(callback, stale, state, _poll_pool(), {"PollVoteFunc": object}, _Manager()))
    assert callback.alerts == [STALE_BUTTON] and callback.message.markups == []


def test_a_poll_that_can_t_be_read_asks_the_option_s_number():
    from bot.routers import activity
    from bot.states import PollVote

    msg, state = _Msg("https://t.me/chan/5"), _State()
    asyncio.run(activity.poll_link(msg, state, _poll_pool(busy=True), {}))
    text, _ = msg.answers[0]
    assert state.state == PollVote.option and text.startswith("ℹ️ Варианты не прочитаны: все воркеры сейчас заняты")
