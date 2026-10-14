"""Offline tests for the bot menu navigation (bot/keyboards/menu.py, bot/services/screens.py, bot/routers/menu.py)."""

import asyncio
import types

from bot.callbacks import ChoiceCB, FunctionCB, MenuAction, MenuCB, NavCB
from bot.keyboards.menu import main_menu, screen_kb
from bot.routers import accounts, menu
from modules.registry import ACCOUNTS, ACTIVITY, PROJECTS, SCREENS, SERVICE, SETTINGS, TASKS, by_category
from bot.services.screens import screen_text


def ns(**kw):
    return types.SimpleNamespace(**kw)


class _State:
    def __init__(self):
        self.cleared = False

    async def clear(self):
        self.cleared = True


class _StateData(_State):
    """A state that keeps its data (the projects' list stores its buttons' names)."""

    def __init__(self):
        super().__init__()
        self.data = {}

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


def test_main_menu_has_the_work_then_the_accounts_and_settings_then_the_jobs():
    rows = [[button.text for button in row] for row in main_menu().keyboard]
    # the audiences: 📁 Проекты; one target: 💬 Активность; what runs now on a row of its own
    assert rows == [[PROJECTS, ACTIVITY], [ACCOUNTS, SETTINGS], [TASKS]]


def test_every_function_has_one_place_and_the_work_on_a_base_is_a_project_s():
    from modules.registry import BOT_FUNCTIONS_BY_KEY

    # a project's card: a group's members as its base, the contacts first — a mailing then writes from
    # that worker — then the check of the scrape's files
    assert [f.key for f in by_category(PROJECTS)] == ["members", "addcontacts", "pmmailing", "verify", "analysis"]
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
    assert "<i>" not in screen_text(SERVICE).split("\n\n<i>⚠️")[0]  # no groups: no subheadings, only the note


def test_hub_screens_pair_only_short_buttons_and_mark_the_submenus():
    from bot.keyboards.menu import SHORT

    rows = [[b.text for b in row] for row in screen_kb(ACCOUNTS).inline_keyboard]
    # the actions, the check on a row of its own, the screens opened from it
    assert rows == [["📋 Список", "➕ Добавить"], ["🌐 Прокси"], ["🛡 Проверка статуса (SpamBot)"],
                    ["👤 Профиль ›", "🛠 Обслуживание ›"]]
    assert all(len(text) <= SHORT for row in rows if len(row) == 2 for text in row)
    submenus = [NavCB.unpack(b.callback_data).screen for b in _buttons(screen_kb(ACCOUNTS))
                if b.callback_data.startswith("nav:")]
    assert submenus == ["👤 Профиль", SERVICE]
    assert [[b.text for b in row] for row in screen_kb(SETTINGS).inline_keyboard] == [
        ["⏱ Задержка", "⏳ Пауза профиля"], ["📬 Автоответ"]]


def test_a_main_menu_button_closes_a_sign_in_waiting_for_its_code(monkeypatch):
    from bot.services import login as logins

    disconnected = []

    async def disconnect():
        disconnected.append(True)

    monkeypatch.setattr(logins, "_logins", {1: ns(timer=None, client=ns(disconnect=disconnect))})
    state = _State()
    asyncio.run(menu.open_from_menu(_Msg(ACTIVITY), state))
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

    callback = _Callback()
    assert asyncio.run(ensure_workers(callback, ns(count=lambda: 0))) is False
    text = callback.message.answers[0][0]
    assert menu_path("add") in text


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
    dp["pool"] = ns(count=lambda: 0)
    dp["settings"] = ns(delay=[1], profile_pause=[60], autoreply_enabled=False, autoreply_text="")
    for module in ROUTERS:
        monkeypatch.setattr(module.router, "_parent_router", None)  # included again, in this dispatcher
    dp.include_routers(*(module.router for module in ROUTERS))
    bot = _OfflineBot()
    key = StorageKey(bot_id=bot.id, chat_id=1, user_id=1)

    steps = [value for group in vars(states).values() if isinstance(group, type)
             for value in vars(group).values() if isinstance(value, State)]
    # one handler opens them all; 📊 Задачи only shows: the form stays (as /status)
    screens = {**{title: [menu.__name__] for title in (PROJECTS, ACTIVITY, ACCOUNTS, SETTINGS)}, TASKS: []}

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
                    ["🚩 Жалоба на пост"], ["🚫 Жалоба на пользователя"]]
    rows = [[b.text for b in row] for row in screen_kb(SERVICE).inline_keyboard]
    assert rows[-1] == ["⬅️ Назад"]  # the way back on a row of its own


def test_risky_functions_are_marked():
    texts = {b.text for b in _buttons(screen_kb(SERVICE))}
    assert "⚠️ 🧹 Очистить диалоги" in texts and "🔒 Установить 2FA" in texts


def test_every_screen_opens_from_its_parent():
    for title, screen in SCREENS.items():
        if screen.parent:
            opens = [NavCB.unpack(b.callback_data).screen for b in _buttons(screen_kb(screen.parent))
                     if b.text == f"{title} ›"]
            assert opens == [title]


def test_activity_button_answers_with_its_functions():
    msg, state = _Msg("💬 Активность"), _State()
    asyncio.run(menu.open_from_menu(msg, state))

    text, markup = msg.answers[0]
    assert text.startswith("<b>💬 Активность</b> ⚠️\n")  # every function risky: marked once, on the title
    assert "\n<b>🚪 Вступить в чат</b> — воркеры вступают" in text  # each item explained
    assert [b.text for b in _buttons(markup)][0] == "✉️ ЛС человеку"
    assert state.cleared


def test_a_mixed_section_marks_only_its_risky_functions():

    text = screen_text(SERVICE)
    assert text.startswith("<b>🛠 Обслуживание</b>\n")
    assert "⚠️ <b>🧹 Очистить диалоги</b>" in text and "\n<b>📴 Сбросить чужие сессии</b>" in text


def test_add_account_offers_phone_for_both_roles_and_tdata():
    from bot.routers import login

    callback, state = _Callback(), _State()
    asyncio.run(login.phone_start(callback, state))
    text, markup = callback.message.answers[0]
    assert state.cleared and [b.text for b in _buttons(markup)] == [
        "🤖 Воркер — по номеру", "👤 Личный — по номеру", "📥 Воркер — из tdata (ZIP)"]
    tdata = _buttons(markup)[2]
    assert MenuCB.unpack(tdata.callback_data).action == MenuAction.TDATA  # the existing tdata upload


class _StepState(_State):
    def __init__(self, step):
        super().__init__()
        self.step = step

    async def get_state(self):
        return None if self.cleared else self.step


def test_cancel_button_leaves_its_own_step():
    from bot.keyboards.common import cancel_kb
    from bot.states import SetDelay

    [button] = _buttons(cancel_kb(SetDelay.input))
    assert button.text == "✖️ Отмена"
    callback, state = _Callback(), _StepState(SetDelay.input.state)
    asyncio.run(menu.cancel_button(callback, ChoiceCB.unpack(button.callback_data), state, ns(active=False)))
    assert state.cleared and callback.message.answers == [("Отменено.", main_menu())]


def test_a_prompt_with_its_own_buttons_has_the_cancel_last():
    from bot.keyboards.common import cancel_kb, choice_kb
    from bot.states import PmMailing

    markup = cancel_kb(PmMailing.path, choice_kb("mail_base", [("📄 A", "0"), ("📄 B", "1")]))
    assert [[b.text for b in row] for row in markup.inline_keyboard] == [["📄 A"], ["📄 B"], ["✖️ Отмена"]]
    assert ChoiceCB.unpack(markup.inline_keyboard[-1][0].callback_data).value == "PmMailing.path"
    assert cancel_kb("PmMailing:path") == cancel_kb(PmMailing.path)  # a step by its name: _check_chats'


def test_cancel_button_of_an_old_prompt_leaves_another_step_alone():
    from bot.keyboards.common import cancel_kb
    from bot.states import SetDelay, SetProfilePause

    [button] = _buttons(cancel_kb(SetDelay.input))
    alerts = []

    async def answer(text=None, **kwargs):
        alerts.append(text)

    callback, state = _Callback(), _StepState(SetProfilePause.input.state)
    callback.answer = answer
    asyncio.run(menu.cancel_button(callback, ChoiceCB.unpack(button.callback_data), state, ns(active=False)))
    assert not state.cleared and callback.message.answers == []
    assert alerts == ["Кнопка устарела — откройте раздел заново."]


def test_a_screen_opens_in_place_of_the_message():
    from bot.services.screens import VIEWS

    for title in SCREENS.keys() - VIEWS.keys():  # as the registry has it
        callback = _Callback()
        asyncio.run(menu.open_by_button(callback, NavCB(screen=title), _State()))
        assert callback.message.answers == []  # no new message
        assert callback.message.edits == [(screen_text(title), screen_kb(title))]
        assert callback.answered
    assert "облачный пароль" in screen_text(SERVICE)


def test_the_projects_list_has_no_work_without_a_project():
    """All the work on a base is a project's: its card's (bot/routers/projects.py)."""
    from bot.routers import projects

    msg = _Msg(PROJECTS)
    asyncio.run(projects.projects_screen(msg, _StateData(), False))
    text, markup = msg.answers[0]
    assert "Без проекта" not in text and not any("Без проекта" in b.text for b in _buttons(markup))
    assert "🧰 Без проекта" not in SCREENS

def test_back_returns_to_accounts_screen_in_place():
    callback = _Callback()
    pool = ns(count=lambda: 5)
    asyncio.run(menu.open_by_button(callback, NavCB(screen=ACCOUNTS), _State(), pool=pool))

    text, markup = callback.message.edits[0]
    assert "воркеров подключено: <b>5</b>" in text
    assert markup == screen_kb(ACCOUNTS)


def test_accounts_screen_with_no_workers_explains_how_to_add():
    msg = _Msg(ACCOUNTS)
    asyncio.run(menu.open_from_menu(msg, _State(), pool=ns(count=lambda: 0)))

    text, markup = msg.answers[0]
    from modules.registry import menu_path

    assert "подключено: <b>0</b>" in text and menu_path("add") in text  # the button there is, not a gone one
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
    asyncio.run(accounts.accounts_screen(msg, _State(), False, ns(count=lambda: 3)))
    text = msg.answers[0][0]
    for button in SCREENS[ACCOUNTS].buttons:
        assert f"<b>{button.title}</b> — {button.hint}" in text
    assert "<b>🛡 Проверка статуса (SpamBot)</b> — спрашивает @SpamBot" in text
    for child in ("👤 Профиль", SERVICE):
        assert f"<b>{child}</b> — {SCREENS[child].hint}" in text
    assert "Задержка" not in text  # a setting: ⚙️ Настройки


def test_settings_screen_shows_the_values_and_explains_its_buttons():
    from bot.routers import settings as screen

    settings = ns(delay=[2, 5], profile_pause=[60, 180], autoreply_enabled=True, autoreply_text="Спасибо!")
    msg = _Msg(SETTINGS)
    asyncio.run(screen.settings_screen(msg, _State(), False, settings))
    text, markup = msg.answers[0]
    assert "задержка <b>2–5 с</b> · пауза профиля <b>60–180 с</b> · автоответ <b>включён</b>\n" in text
    for button in SCREENS[SETTINGS].buttons:
        assert f"<b>{button.title}</b> — {button.hint}" in text
    assert markup == screen_kb(SETTINGS)


def test_a_saved_setting_brings_back_the_settings_screen_with_the_new_value():
    from bot.routers import settings as screen

    settings = ns(delay=[2, 5], profile_pause=[60, 180], autoreply_enabled=False, autoreply_text="")
    settings.set_delay = lambda value: setattr(settings, "delay", value)
    settings.set_profile_pause = lambda value: setattr(settings, "profile_pause", value)

    msg = _Msg("3-6")
    asyncio.run(screen.delay_apply(msg, _State(), settings))
    text, markup = msg.answers[0]
    assert text.startswith("✅ Задержка: 3–6 с.") and "задержка <b>3–6 с</b>" in text
    assert markup == screen_kb(SETTINGS)

    msg = _Msg("120")
    asyncio.run(screen.profilepause_apply(msg, _State(), settings))
    text, markup = msg.answers[0]
    assert text.startswith("✅ Пауза профиля: 120 с.") and "пауза профиля <b>120 с</b>" in text
    assert markup == screen_kb(SETTINGS)


def test_start_names_every_main_menu_button():
    from modules.registry import MAIN_MENU

    text = menu.welcome("Ann", 2)
    assert MAIN_MENU == (PROJECTS, ACTIVITY, ACCOUNTS, SETTINGS, TASKS)
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
    msg = _Msg("/status")
    asyncio.run(menu.status(msg, manager, scrapes, analyses))
    assert msg.answers == [("Сейчас ничего не выполняется.", None)]

    manager.acquire("Рассылка", timeout=0)
    manager.progress = Progress()
    scrapes.acquire("Скрап", cancelable=False, timeout=0)
    scrapes.progress = Progress()
    analyses.acquire("Анализ", cancelable=False, timeout=0)  # no progress, no buttons
    msg = _Msg("/status")
    asyncio.run(menu.status(msg, manager, scrapes, analyses))  # no state: a form under way is not left
    assert [markup for _, markup in msg.answers] == [stop_kb(), scrape_kb(), None]
    assert msg.answers[0][0] == manager.progress.render("Рассылка") and msg.answers[2][0] == "▶️ Анализ"


def test_screen_navigation_closes_a_sign_in_waiting_for_its_code(monkeypatch):
    from bot.routers import _common

    closed = []

    async def close(chat_id):
        closed.append(chat_id)

    monkeypatch.setattr(_common.logins, "close", close)
    asyncio.run(menu.open_by_button(_Callback(), NavCB(screen=SERVICE), _State()))
    asyncio.run(menu.open_by_button(_Callback(), NavCB(screen=ACCOUNTS), _State(), pool=ns(count=lambda: 1)))
    assert closed == [1, 1]  # as a main-menu button: not left open until its timeout


def test_an_old_navigation_button_is_stale_and_a_new_one_opens_its_screen(monkeypatch):
    """Through the real dispatcher: the buttons of the menu before the screens' tree (a group by its
    index, ⬅️ to a hub) reach the last router only; a screen's button opens it."""
    import datetime

    from aiogram import Bot, Dispatcher
    from aiogram.methods import AnswerCallbackQuery, EditMessageText
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
        # and those of 🧰 Без проекта, gone: its screen, its functions (now a project card's)
        old = ("cat:0", "cat:3", "menu:workers", "menu:projects", "menu:settings",
               NavCB(screen="🧰 Без проекта").pack(), NavCB(screen="🤖 Воркеры").pack(),  # renamed: 🤖 Аккаунты
               *(FunctionCB(key=key).pack() for key in ("members", "addcontacts", "pmmailing", "verify", "analysis")))
        for data in old:
            assert asyncio.run(tap(data)) == ([AnswerCallbackQuery], [STALE_BUTTON]), data
        assert asyncio.run(tap(NavCB(screen=SERVICE).pack()))[0] == [EditMessageText, AnswerCallbackQuery]
    finally:
        for module in ROUTERS:
            module.router._parent_router = None


def test_the_screens_with_live_values_or_of_their_own_kind_register_their_views():
    import bot.app  # noqa: F401 — every router imported, as at the bot's start
    from bot.services.screens import VIEWS

    assert set(VIEWS) == {PROJECTS, ACCOUNTS, SETTINGS, TASKS}  # the rest: as the registry has them


def test_the_tasks_button_shows_what_runs_and_keeps_the_form():
    from bot.services.jobs import JobManager

    msg, state = _Msg(TASKS), _State()
    asyncio.run(menu.open_from_menu(msg, state, manager=JobManager(), scrapes=JobManager(), analyses=JobManager()))
    assert msg.answers == [("Сейчас ничего не выполняется.", None)]  # as /status
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
    from bot.services.jobs import JobManager
    from bot.states import Projects

    sent = []

    class Offline(Bot):
        async def __call__(self, method, request_timeout=None):
            if isinstance(method, SendMessage):
                sent.append(method.text)

    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    dp["manager"], dp["scrapes"], dp["analyses"] = JobManager(), JobManager(), JobManager()
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
    assert sent == ["Сейчас ничего не выполняется."]
