"""Offline tests for the bot menu navigation (bot/keyboards/menu.py, bot/services/screens.py, bot/routers/menu.py)."""

import asyncio
import conftest
import types

from bot.callbacks import ChoiceCB, FunctionCB, MenuAction, MenuCB, NavCB
from bot.keyboards.menu import back_kb, main_menu, screen_kb
from bot.routers import accounts, menu
from modules.registry import (ACCOUNTS, ACTIVITY, AUTOREPLY, BASE, CARD, COLLECT, LIMITS, MANAGE, PROJECTS, SCREENS,
                              SECURITY, SETTINGS, TASKS, by_category, in_project)
from bot.services.screens import screen_text


def ns(**kw):
    return types.SimpleNamespace(**kw)


class _State:
    def __init__(self):
        self.cleared = False

    async def clear(self):
        self.cleared = True

    async def get_data(self):
        return {}


class _StateData(_State):
    """A state that keeps its data (the projects' list stores its buttons' names)."""

    def __init__(self, data=None):
        super().__init__()
        self.data = dict(data or {})

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_data(self):
        return dict(self.data)


class _Msg:
    def __init__(self, text=None):
        self.text, self.chat = text, ns(id=1)
        self.answers, self.edits = [], []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs.get("reply_markup")))

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs.get("reply_markup")))


class _Callback:
    def __init__(self):
        self.message = _Msg()
        self.answered = False

    async def answer(self, *args, **kwargs):
        self.answered = True


def _buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


def test_main_menu_has_the_projects_and_the_settings_then_the_jobs():
    rows = [[button.text for button in row] for row in main_menu().keyboard]
    # all the work is a project's: its accounts and their activity are on its card; what runs now on a row of its own
    assert rows == [[PROJECTS, SETTINGS], [TASKS]]
    assert SCREENS[ACCOUNTS].parent == SCREENS[ACTIVITY].parent == CARD and SCREENS[CARD].parent == PROJECTS


def test_every_function_has_one_place_and_the_work_on_a_base_is_a_project_s():
    from modules.registry import BOT_FUNCTIONS_BY_KEY

    # a project's card's screens on its files: a group's members as its base, then the check of the scrape's
    # files; the contacts first — a mailing then writes from that worker
    assert [f.key for f in by_category(COLLECT)] == ["members", "verify", "analysis"]
    assert [f.key for f in by_category(BASE)] == ["addcontacts", "pmmailing"]
    assert by_category(PROJECTS) == []
    assert [f.key for f in by_category(ACTIVITY)][:4] == ["pm", "comments", "instant", "chat"]  # one target
    assert "invite" in [f.key for f in by_category(ACTIVITY)]  # chat to chat: no base, no project
    assert [f.key for f in by_category(ACCOUNTS)] == ["status"]  # the check most needed: on the screen itself
    assert "scrape" not in BOT_FUNCTIONS_BY_KEY  # ▶️ on a project's card only


def test_screen_kb_lists_its_functions_in_order_then_the_way_back():
    buttons = _buttons(screen_kb("👤 Профиль"))
    keys = [FunctionCB.unpack(b.callback_data).key for b in buttons if b.callback_data.startswith("fn:")]
    assert keys == [f.key for f in by_category("👤 Профиль")]
    assert buttons[-1].text == "⬅️ Назад"  # the screen opens from the accounts'
    assert NavCB.unpack(buttons[-1].callback_data).screen == ACCOUNTS


def test_sections_explain_their_groups():
    from modules.registry import IN_CHATS, ONE_TARGET, REPORTS

    activity = screen_text(ACTIVITY)
    assert activity.index(f"<i>{ONE_TARGET}</i>") < activity.index("✉️ ЛС человеку") \
        < activity.index(f"<i>{IN_CHATS}</i>") < activity.index("🗳 Голосовать") < activity.index("🤝 Инвайтинг") \
        < activity.index(f"<i>{REPORTS}</i>") < activity.index("🚩 Жалоба")
    assert "<i>" not in screen_text(SECURITY).split("\n\n<i>⚠️")[0]  # no groups: no subheadings, only the note


def test_hub_screens_pair_only_short_buttons_and_mark_the_submenus():
    from bot.keyboards.menu import SHORT

    rows = [[b.text for b in row] for row in screen_kb(ACCOUNTS).inline_keyboard]
    # the actions, the check on a row of its own, the screens opened from it
    assert rows == [["📋 Список", "➕ Добавить"], ["🌐 Прокси", "↪️ Перенести"], ["🛡 Проверка статуса (SpamBot)"],
                    ["👤 Профиль ›", "🔐 Безопасность ›"], ["⬅️ Назад"]]
    assert all(len(text) <= SHORT for row in rows if len(row) == 2 for text in row)
    submenus = [NavCB.unpack(b.callback_data).screen for b in _buttons(screen_kb(ACCOUNTS))
                if b.callback_data.startswith("nav:") and b.text.endswith(" ›")]
    assert submenus == ["👤 Профиль", SECURITY]
    assert [[b.text for b in row] for row in screen_kb(SETTINGS).inline_keyboard] == [
        ["⏱ Задержка", "⏳ Пауза профиля"], ["📬 Автоответ ›", "📏 Дневные лимиты ›"]]


def test_the_card_holds_its_screens_two_in_a_row_each_acting_on_its_project():
    rows = [[b.text for b in row] for row in screen_kb(CARD, 7).inline_keyboard]
    assert rows == [["📥 Сбор ›", "📨 База и рассылка ›"], ["🤖 Аккаунты ›", "💬 Активность ›"],
                    ["⚙️ Управление ›"], ["⬅️ Назад"]]
    *screens, back = _buttons(screen_kb(CARD, 7))
    assert {NavCB.unpack(b.callback_data).project for b in screens} == {7}
    assert NavCB.unpack(back.callback_data) == NavCB(screen=PROJECTS)  # the list: no project's
    # a screen's own buttons carry it too: they act on the project they were shown for
    assert {MenuCB.unpack(b.callback_data).project for b in _buttons(screen_kb(COLLECT, 7))
            if b.callback_data.startswith("menu:")} == {7}
    assert {FunctionCB.unpack(b.callback_data).project for b in _buttons(screen_kb(ACTIVITY, 7))
            if b.callback_data.startswith("fn:")} == {7}


def test_a_main_menu_button_closes_a_sign_in_waiting_for_its_code(monkeypatch):
    from bot.services import login as logins

    disconnected = []

    async def disconnect():
        disconnected.append(True)

    monkeypatch.setattr(logins, "_logins", {1: ns(timer=None, client=ns(disconnect=disconnect))})
    state = _State()
    asyncio.run(menu.open_from_menu(_Msg(SETTINGS), state, settings=ns(delay=[1], profile_pause=[60],
                                                                       trigger="", messages_count=0,
                                                                       autoreply_enabled=False, autoreply_text="")))
    assert logins._logins == {} and disconnected and state.cleared  # as /cancel: now, not after its timeout


def test_a_button_nothing_takes_is_answered():
    from bot.app import ROUTERS
    from bot.routers import stale

    assert ROUTERS[-1] is stale  # after every other handler
    alerts = []

    async def answer(text=None, **kwargs):
        alerts.append(text)

    asyncio.run(stale.stale_button(ns(answer=answer)))
    assert alerts == ["Кнопка устарела — откройте раздел заново."]


def test_a_message_outside_any_step_gets_the_menu():
    from aiogram.filters import StateFilter

    from bot.routers import stale

    msg = _Msg("привет")
    asyncio.run(stale.no_step(msg))
    assert msg.answers == [("Выберите раздел в меню ниже 👇", main_menu())]
    [handler] = [h for h in stale.router.message.handlers if h.callback is stale.no_step]
    # in a step (one waiting for a button or a file) the message is not answered with the menu
    assert [f.callback for f in handler.filters if isinstance(f.callback, StateFilter)][0].states == (None,)


def test_a_message_in_a_step_that_waits_for_a_button_is_answered():
    """Every step's message handler comes before it, so only a step with none (a button's) gets here."""
    from bot.routers import stale

    msg = _Msg("да")
    asyncio.run(stale.button_step(msg))
    assert msg.answers == [("Нажмите кнопку выше — или /cancel, чтобы выйти.", None)]
    handlers = [h.callback for h in stale.router.message.handlers]
    assert handlers.index(stale.no_step) < handlers.index(stale.button_step)  # outside a step: the menu


def test_the_bot_s_commands_are_set_and_a_failure_does_not_stop_the_start():
    from bot.app import set_commands

    class _Bot:
        def __init__(self, fail=False):
            self.fail, self.commands = fail, None

        async def set_my_commands(self, commands):
            if self.fail:
                raise RuntimeError("Bad Gateway")
            self.commands = [command.command for command in commands]

    bot = _Bot()
    asyncio.run(set_commands(bot))
    assert bot.commands == ["start", "status", "cancel"]
    asyncio.run(set_commands(_Bot(fail=True)))  # only logged


def test_no_workers_names_where_to_add_them():
    from bot.routers._common import ensure_workers
    from modules.registry import menu_path

    alerts = []

    async def answer(text=None, show_alert=False):
        alerts.append((text, show_alert))

    callback = _Callback()
    callback.answer = answer
    assert asyncio.run(ensure_workers(callback, ns(count=lambda: 0))) is False
    [(text, popup)] = alerts  # over the screen: no message piled up under it
    assert popup and menu_path("add") in text and not callback.message.answers


def test_a_main_menu_button_leaves_any_form():
    """The menu's router opens every screen and comes before any other's input steps, so a main-menu
    button opens its screen instead of being taken as input."""
    from bot.app import ROUTERS
    from bot.routers import projects, settings

    assert ROUTERS[:4] == (menu, accounts, projects, settings)


class _OfflineBot:
    """A Bot whose API calls go nowhere: the handlers' answers are not what is tested."""

    def __new__(cls):
        from aiogram import Bot

        class Offline(Bot):
            async def __call__(self, method, request_timeout=None):
                return None

        return Offline("42:TEST")


def test_a_main_menu_button_in_any_step_opens_its_screen(monkeypatch):
    """Through the real dispatcher: in every step of every flow — those of the first routers too
    (a project's name, a tdata 2FA password) — a main-menu button opens its screen, never is the input."""
    import datetime

    from aiogram import Dispatcher
    from aiogram.fsm.state import State
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.types import Chat, Message, Update, User

    from bot import states
    from bot.app import ROUTERS

    left = []

    def leave(module):
        async def leave_form(message, state):
            left.append(module)
            await state.clear()
        return leave_form

    monkeypatch.setattr(menu, "leave_form", leave(menu.__name__))

    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    dp["workspaces"] = conftest.Workspaces()
    dp["settings"] = ns(delay=[1], profile_pause=[60], trigger="go", messages_count=0,
                  autoreply_enabled=False, autoreply_text="")
    for module in ROUTERS:
        monkeypatch.setattr(module.router, "_parent_router", None)  # included again, in this dispatcher
    dp.include_routers(*(module.router for module in ROUTERS))
    bot = _OfflineBot()
    key = StorageKey(bot_id=bot.id, chat_id=1, user_id=1)

    steps = [value for group in vars(states).values() if isinstance(group, type)
             for value in vars(group).values() if isinstance(value, State)]
    # one handler opens them all; 📊 Задачи only shows: the form stays (as /status)
    screens = {**{title: [menu.__name__] for title in (PROJECTS, SETTINGS)}, TASKS: []}

    async def press(step, button):
        await storage.set_state(key, step)
        left.clear()
        message = Message(message_id=1, date=datetime.datetime.now(), chat=Chat(id=1, type="private"),
                          from_user=User(id=1, is_bot=False, first_name="x"), text=button)
        try:
            await dp.feed_update(bot, Update(update_id=1, message=message))
        except Exception:  # a step's handler took it (and missed its other dependencies): asserted below
            pass
        return list(left)

    async def press_all():
        return [(step.state, button) for step in steps for button, screen in screens.items()
                if await press(step, button) != screen]

    try:
        wrong = asyncio.run(press_all())
    finally:
        for module in ROUTERS:
            module.router._parent_router = None
    assert wrong == []


def test_back_button_leads_to_the_parent_and_only_off_the_main_menu():
    for title, screen in SCREENS.items():
        back = [b for b in _buttons(screen_kb(title)) if "Назад" in b.text]
        if screen.parent:
            assert back == [_buttons(screen_kb(title))[-1]], title
            assert NavCB.unpack(back[0].callback_data).screen == screen.parent
        else:  # a main-menu screen
            assert back == [], title


def test_screen_kb_rows_follow_the_subheadings():
    # two short ones in a row, each subheading on a new row
    rows = [[b.text for b in row] for row in screen_kb(ACTIVITY).inline_keyboard]
    assert rows == [["✉️ ЛС человеку", "🗨 Комментарии"], ["⚡ Чат — сразу", "🔔 Чат — по слову"],
                    ["🚪 Вступить в чат", "❤️ Реакции"], ["🗳 Голосовать", "🤝 Инвайтинг"],
                    ["🚩 Жалоба на пост"], ["🚫 Жалоба на пользователя"], ["⬅️ Назад"]]
    rows = [[b.text for b in row] for row in screen_kb(SECURITY).inline_keyboard]
    assert rows[-1] == ["⬅️ Назад"]  # the way back on a row of its own


def test_risky_functions_are_marked():
    texts = {b.text for b in _buttons(screen_kb(SECURITY))}
    assert "⚠️ 🧹 Очистить диалоги" in texts and "🔒 Установить 2FA" in texts


def test_every_screen_opens_from_its_parent():
    for title, screen in SCREENS.items():
        if screen.parent:
            opens = [NavCB.unpack(b.callback_data).screen for b in _buttons(screen_kb(screen.parent))
                     if b.text == f"{title} ›"]
            assert opens == [title]


def test_activity_button_answers_with_its_functions():
    msg, state = _Msg("💬 Активность"), _State()
    asyncio.run(menu.open_from_menu(msg, state))  # no project opened: none of its work
    assert msg.answers == [("⛔ Сначала откройте проект: 📁 Проекты → проект.", back_kb(PROJECTS))]
    assert not state.cleared

    msg = _Msg("💬 Активность")
    asyncio.run(menu.open_from_menu(msg, state, workspace=conftest.Workspaces().workspace))
    text, markup = msg.answers[0]
    assert text.startswith("<b>💬 Активность</b> ⚠️\n")  # every function risky: marked once, on the title
    assert "\n<b>🚪 Вступить в чат</b> — воркеры вступают" in text  # each item explained
    assert [b.text for b in _buttons(markup)][0] == "✉️ ЛС человеку"
    assert state.cleared


def test_a_mixed_section_marks_only_its_risky_functions():

    text = screen_text(SECURITY)
    assert text.startswith("<b>🔐 Безопасность</b>\n")
    assert "⚠️ <b>🧹 Очистить диалоги</b>" in text and "\n<b>📴 Сбросить чужие сессии</b>" in text


def test_add_account_offers_phone_for_both_roles_and_tdata():
    from bot.routers import login

    callback, state = _Callback(), _State()
    asyncio.run(login.phone_start(callback, state, ns(project_id=4)))
    text, markup = callback.message.edits[0]  # in 🤖 Аккаунты' place, ⬅️ back to it
    assert state.cleared and [b.text for b in _buttons(markup)] == [
        "🤖 Воркер — по номеру", "👤 Личный — по номеру", "📥 Воркер — из tdata (ZIP)", "📦 Воркеры — из свободных",
        "⬅️ Назад"]
    assert NavCB.unpack(_buttons(markup)[-1].callback_data) == NavCB(screen=ACCOUNTS, project=4)
    tdata = _buttons(markup)[2]
    assert MenuCB.unpack(tdata.callback_data) == MenuCB(action=MenuAction.TDATA, project=4)  # into its project


class _StepState(_State):
    def __init__(self, step):
        super().__init__()
        self.step = step

    async def get_state(self):
        return None if self.cleared else self.step


def test_cancel_button_leaves_its_own_step():
    from bot.keyboards.common import cancel_kb
    from bot.states import SetSetting

    [button] = _buttons(cancel_kb(SetSetting.value))
    assert button.text == "✖️ Отмена"
    callback, state = _Callback(), _StepState(SetSetting.value.state)
    asyncio.run(menu.cancel_button(callback, ChoiceCB.unpack(button.callback_data), state, ns(active=False)))
    # in the prompt's place, back to the screen the form was started from
    assert state.cleared and callback.message.edits == [("Отменено.", back_kb(SETTINGS))]


def test_a_prompt_with_its_own_buttons_has_the_cancel_last():
    from bot.keyboards.common import cancel_kb, choice_kb
    from bot.states import PmMailing

    markup = cancel_kb(PmMailing.path, choice_kb("mail_base", [("📄 A", "0"), ("📄 B", "1")]))
    assert [[b.text for b in row] for row in markup.inline_keyboard] == [["📄 A"], ["📄 B"], ["✖️ Отмена"]]
    assert ChoiceCB.unpack(markup.inline_keyboard[-1][0].callback_data).value == "PmMailing.path"
    assert cancel_kb("PmMailing:path") == cancel_kb(PmMailing.path)  # a step by its name: _check_chats'


def test_cancel_button_of_an_old_prompt_leaves_another_step_alone():
    from bot.keyboards.common import cancel_kb
    from bot.states import SetAutoreplyText, SetSetting

    [button] = _buttons(cancel_kb(SetSetting.value))
    alerts = []

    async def answer(text=None, **kwargs):
        alerts.append(text)

    callback, state = _Callback(), _StepState(SetAutoreplyText.input.state)
    callback.answer = answer
    asyncio.run(menu.cancel_button(callback, ChoiceCB.unpack(button.callback_data), state, ns(active=False)))
    assert not state.cleared and callback.message.answers == []
    assert alerts == ["Кнопка устарела — откройте раздел заново."]


def test_a_screen_opens_in_place_of_the_message():
    from bot.services.screens import VIEWS

    for title in SCREENS.keys() - VIEWS.keys():  # as the registry has it
        callback = _Callback()
        workspace = conftest.Workspaces().workspace
        project = {"workspace": workspace} if in_project(title) else {}
        asyncio.run(menu.open_by_button(callback, NavCB(screen=title), _State(), **project))
        assert callback.message.answers == []  # no new message
        shown = (screen_text(title), screen_kb(title, workspace.project_id if project else 0))
        assert callback.message.edits == [shown]
        assert callback.answered
    assert "облачный пароль" in screen_text(SECURITY)


def test_the_projects_list_has_no_work_without_a_project():
    """All the work on a base is a project's: its card's (bot/routers/projects/)."""
    from bot.routers.projects import listing

    msg = _Msg(PROJECTS)
    asyncio.run(listing.projects_screen(msg, _StateData(), False))
    text, markup = msg.answers[0]
    assert "Без проекта" not in text and not any("Без проекта" in b.text for b in _buttons(markup))
    assert "🧰 Без проекта" not in SCREENS

def test_back_returns_to_accounts_screen_in_place():
    callback = _Callback()
    pool = ns(count=lambda: 5)
    asyncio.run(menu.open_by_button(callback, NavCB(screen=ACCOUNTS), _State(), pool=pool, workspace=ns()))

    text, markup = callback.message.edits[0]
    assert "Аккаунты проекта</b> — воркеров: <b>5</b>" in text
    assert markup == screen_kb(ACCOUNTS)


def test_accounts_screen_with_no_workers_explains_how_to_add():
    msg = _Msg(ACCOUNTS)
    asyncio.run(menu.open_from_menu(msg, _State(), pool=ns(count=lambda: 0), workspace=ns()))

    text, markup = msg.answers[0]
    from modules.registry import menu_path

    assert "воркеров: <b>0</b>" in text and menu_path("add") in text  # the button there is, not a gone one
    assert markup == screen_kb(ACCOUNTS)  # import stays reachable with zero workers


def test_every_function_is_explained_in_its_section():
    from modules.registry import BOT_FUNCTIONS, RISK_NOTE

    assert all(function.hint for function in BOT_FUNCTIONS)
    for category in SCREENS:
        text = screen_text(category)
        risky = any(f.risk == "risky" for f in BOT_FUNCTIONS if f.category == category)
        assert all(f.hint.replace("«", "").split()[0] in text.replace("«", "")
                   for f in BOT_FUNCTIONS if f.category == category)
        assert (RISK_NOTE in text) == risky


def test_accounts_screen_explains_its_buttons():
    msg = _Msg(ACCOUNTS)
    asyncio.run(accounts.workers.accounts_screen(msg, _State(), False, ns(count=lambda: 3)))
    text = msg.answers[0][0]
    for button in SCREENS[ACCOUNTS].buttons:
        assert f"<b>{button.title}</b> — {button.hint}" in text
    assert "<b>🛡 Проверка статуса (SpamBot)</b> — спрашивает @SpamBot" in text
    for child in ("👤 Профиль", SECURITY):
        assert f"<b>{child}</b> — {SCREENS[child].hint}" in text
    assert "Задержка" not in text  # a setting: ⚙️ Настройки


def test_settings_screen_shows_the_values_and_explains_its_buttons():
    from bot.routers import settings as screen

    settings = ns(delay=[2, 5], profile_pause=[60, 180], trigger="go", messages_count=0,
                  autoreply_enabled=True, autoreply_text="Спасибо!")
    msg = _Msg(SETTINGS)
    asyncio.run(screen.settings_screen(msg, _State(), False, settings))
    text, markup = msg.answers[0]
    assert "задержка <b>2–5 с</b> · пауза профиля <b>60–180 с</b> · автоответ <b>включён</b>\n" in text
    for button in SCREENS[SETTINGS].buttons:
        assert f"<b>{button.title}</b> — {button.hint}" in text
    assert markup == screen_kb(SETTINGS)


def test_a_saved_setting_brings_back_the_settings_screen_with_the_new_value():
    from bot.routers import settings as screen

    settings = ns(delay=[2, 5], profile_pause=[60, 180], trigger="go", messages_count=0,
                  autoreply_enabled=False, autoreply_text="")
    settings.set_delay = lambda value: setattr(settings, "delay", value)
    settings.set_profile_pause = lambda value: setattr(settings, "profile_pause", value)

    msg = _Msg("3-6")
    asyncio.run(screen.setting_typed(msg, _StateData({"setting": "delay"}), settings))
    text, markup = msg.answers[0]
    assert text.startswith("✅ Задержка: 3–6 с.") and "задержка <b>3–6 с</b>" in text
    assert markup == screen_kb(SETTINGS)

    msg = _Msg("120")
    asyncio.run(screen.setting_typed(msg, _StateData({"setting": "ppause"}), settings))
    text, markup = msg.answers[0]
    assert text.startswith("✅ Пауза профиля: 120 с.") and "пауза профиля <b>120 с</b>" in text
    assert markup == screen_kb(SETTINGS)


def test_start_names_every_main_menu_button():
    from modules.registry import MAIN_MENU

    text = menu.welcome("Ann", 1, 2)
    assert "📁 Проектов: <b>1</b> · 🤖 воркеров: <b>2</b>" in text
    assert MAIN_MENU == (PROJECTS, SETTINGS, TASKS)
    for label in MAIN_MENU:
        assert f"<b>{label.split(' ', 1)[1]}</b> — {SCREENS[label].hint}" in text


def test_photo_folder_button_works_only_in_its_step():
    from bot.routers import profile
    from bot.states import ChangePhoto

    [handler] = [h for h in profile.router.callback_query.handlers if h.callback is profile.photo_folder]
    # an old message's button starts nothing
    assert ChangePhoto.photo in [f.callback for f in handler.filters]


def test_status_shows_each_running_job_with_its_buttons_and_keeps_the_form():
    from bot.keyboards.common import scrape_kb, stop_kb
    from bot.services.jobs import JobManager
    from bot.services.progress import Progress

    manager, scrapes, analyses = JobManager(), JobManager(), JobManager()
    workspaces = conftest.Workspaces(scrapes=scrapes, manager=manager, analyses=analyses)
    msg = _Msg("/status")
    asyncio.run(menu.status(msg, workspaces))
    assert msg.answers == [("📊 Задачи — сейчас ничего не выполняется.", None)]

    manager.acquire("Рассылка", timeout=0)
    manager.progress = Progress()
    scrapes.acquire("Скрап", cancelable=False, timeout=0)
    scrapes.progress = Progress()
    analyses.acquire("Анализ", cancelable=False, timeout=0)  # no progress, no buttons
    msg = _Msg("/status")
    asyncio.run(menu.status(msg, workspaces))  # no state: a form under way is not left
    # each with its project's name (none in the file here) and its own slot's buttons
    jobs = msg.answers  # the header on the first one's message, not a message of its own
    assert [markup for _, markup in jobs] == [stop_kb(manager.key), scrape_kb(scrapes.key), None]
    assert jobs[0][0] == "📊 Задачи — выполняется: 3\n\n" + manager.progress.render("📁 ? · Рассылка")
    assert jobs[2][0] == "▶️ 📁 ? · Анализ"


def test_screen_navigation_closes_a_sign_in_waiting_for_its_code(monkeypatch):
    from bot.routers import _common

    closed = []

    async def close(chat_id):
        closed.append(chat_id)

    monkeypatch.setattr(_common.logins, "close", close)
    asyncio.run(menu.open_by_button(_Callback(), NavCB(screen=SECURITY), _State(), workspace=ns(project_id=0)))
    asyncio.run(menu.open_by_button(_Callback(), NavCB(screen=ACCOUNTS), _State(), pool=ns(count=lambda: 1),
                                    workspace=ns(project_id=0)))
    assert closed == [1, 1]  # as a main-menu button: not left open until its timeout


def test_an_old_navigation_button_is_stale_and_a_new_one_opens_its_screen(monkeypatch):
    """Through the real dispatcher: the buttons of the menu before the screens' tree (a group by its
    index, ⬅️ to a hub) reach the last router only; a screen's button opens it."""
    import datetime

    from aiogram import Bot, Dispatcher
    from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
    from aiogram.types import CallbackQuery, Chat, Message, Update, User

    from bot.app import ROUTERS
    from bot.routers._common import STALE_BUTTON

    calls = []

    class Offline(Bot):
        async def __call__(self, method, request_timeout=None):
            calls.append(method)

    dp = Dispatcher()
    for module in ROUTERS:
        monkeypatch.setattr(module.router, "_parent_router", None)  # included again, in this dispatcher
    dp.include_routers(*(module.router for module in ROUTERS))
    bot = Offline("42:TEST")
    user = User(id=1, is_bot=False, first_name="x")
    message = Message(message_id=1, date=datetime.datetime.now(), chat=Chat(id=1, type="private"), from_user=user,
                      text="…")

    async def tap(data):
        calls.clear()
        callback = CallbackQuery(id="1", from_user=user, chat_instance="1", data=data, message=message)
        await dp.feed_update(bot, Update(update_id=1, callback_query=callback))
        return [type(call) for call in calls], [call.text for call in calls if isinstance(call, AnswerCallbackQuery)]

    try:
        # and those of 🧰 Без проекта, gone: its screen; a card's buttons by its token, before its screens;
        # the chat campaign's settings, now asked by its flow only
        old = ("cat:0", "cat:3", "menu:workers", "menu:projects", "menu:settings", "menu:trigger", "menu:count",
               NavCB(screen="🧰 Без проекта").pack(), NavCB(screen="🤖 Воркеры").pack(),  # renamed: 🤖 Аккаунты
               ChoiceCB(scope="prj_act", value="scrape.t0ken").pack())
        for data in old:
            assert asyncio.run(tap(data)) == ([AnswerCallbackQuery], [STALE_BUTTON]), data
        # a project's screen: in the chat's current project only (bot/services/workspaces.ProjectMiddleware)
        assert asyncio.run(tap(NavCB(screen=SECURITY).pack()))[0] == [SendMessage, AnswerCallbackQuery]
        dp["workspace"] = conftest.Workspaces().workspace
        assert asyncio.run(tap(NavCB(screen=SECURITY).pack()))[0] == [EditMessageText, AnswerCallbackQuery]
    finally:
        for module in ROUTERS:
            module.router._parent_router = None


def test_the_screens_with_live_values_or_of_their_own_kind_register_their_views():
    import bot.app  # noqa: F401 — every router imported, as at the bot's start
    from bot.services.screens import VIEWS

    # the rest: as the registry has them (a project's: with its name)
    assert set(VIEWS) == {PROJECTS, CARD, COLLECT, MANAGE, ACCOUNTS, SETTINGS, LIMITS, AUTOREPLY, TASKS}


def test_the_tasks_button_shows_what_runs_and_keeps_the_form():

    msg, state = _Msg(TASKS), _State()
    asyncio.run(menu.open_from_menu(msg, state, workspaces=conftest.Workspaces()))
    assert msg.answers == [("📊 Задачи — сейчас ничего не выполняется.", None)]  # as /status
    assert not state.cleared


def test_the_tasks_button_in_a_form_s_step_is_not_its_input(monkeypatch):
    """Through the real dispatcher: 📊 Задачи in a step that takes any text answers with the jobs, and
    the chat stays at that step."""
    import datetime

    from aiogram import Bot, Dispatcher
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.methods import SendMessage
    from aiogram.types import Chat, Message, Update, User

    from bot.app import ROUTERS
    from bot.states import Projects

    sent = []

    class Offline(Bot):
        async def __call__(self, method, request_timeout=None):
            if isinstance(method, SendMessage):
                sent.append(method.text)

    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    dp["workspaces"] = conftest.Workspaces()
    for module in ROUTERS:
        monkeypatch.setattr(module.router, "_parent_router", None)  # included again, in this dispatcher
    dp.include_routers(*(module.router for module in ROUTERS))
    bot = Offline("42:TEST")
    key = StorageKey(bot_id=bot.id, chat_id=1, user_id=1)
    message = Message(message_id=1, date=datetime.datetime.now(), chat=Chat(id=1, type="private"),
                      from_user=User(id=1, is_bot=False, first_name="x"), text=TASKS)

    async def tap():
        await storage.set_state(key, Projects.name)  # a project's name: any text
        await dp.feed_update(bot, Update(update_id=1, message=message))
        return await storage.get_state(key)

    try:
        assert asyncio.run(tap()) == Projects.name.state
    finally:
        for module in ROUTERS:
            module.router._parent_router = None
    assert sent == ["📊 Задачи — сейчас ничего не выполняется."]
