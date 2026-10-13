"""Offline tests for the bot menu navigation (bot/keyboards/menu.py, bot/routers/menu.py, accounts.py)."""

import asyncio
import types

from bot.callbacks import CategoryCB, FunctionCB, MenuAction, MenuCB
from bot.keyboards.menu import PROJECTS_BUTTON, SETTINGS_BUTTON, WORKERS_BUTTON, functions_kb, main_menu, screen_kb
from bot.routers import accounts, menu
from bot.services.registry import SCREENS, SECTIONS, SETTINGS, WORKER_GROUPS, WORKERS, by_category


def ns(**kw):
    return types.SimpleNamespace(**kw)


class _State:
    def __init__(self):
        self.cleared = False

    async def clear(self):
        self.cleared = True


class _Msg:
    def __init__(self, text=None):
        self.text = text
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


def test_main_menu_has_the_projects_then_the_sections_then_the_two_screens():
    rows = [[button.text for button in row] for row in main_menu().keyboard]
    assert rows == [[PROJECTS_BUTTON], list(SECTIONS), [WORKERS_BUTTON, SETTINGS_BUTTON]]
    assert not any("Проекты" in b.text for b in _buttons(functions_kb("🔍 Сбор данных")))  # not in a section


def test_contacts_lead_the_mailings_and_data_collection_is_only_collecting():
    from bot.services.registry import BOT_FUNCTIONS

    assert [f.key for f in by_category("📣 Рассылки")][0] == "addcontacts"  # added to contacts, then mailed
    assert [f.key for f in by_category("🔍 Сбор данных")] == ["scrape", "members", "verify", "analysis"]
    assert not any("Аудитория" in f.category for f in BOT_FUNCTIONS)


def test_functions_kb_lists_category_in_order():
    keys = [FunctionCB.unpack(b.callback_data).key for b in _buttons(functions_kb("📣 Рассылки"))]
    assert keys == [f.key for f in by_category("📣 Рассылки")]


def test_back_button_only_in_worker_groups():
    assert not any("Назад" in b.text for b in _buttons(functions_kb("📣 Рассылки")))
    back = _buttons(functions_kb("👤 Профиль", back=True))[-1]
    assert MenuCB.unpack(back.callback_data).action == MenuAction.WORKERS


def test_risky_functions_are_marked():
    texts = {b.text for b in _buttons(functions_kb("🩺 Проверка и обслуживание"))}
    assert "⚠️ 🧹 Очистить диалоги" in texts and "🌍 Статистика по номерам" in texts


def test_workers_screen_opens_every_group():
    groups = [CategoryCB.unpack(b.callback_data).index
              for b in _buttons(screen_kb(WORKERS)) if b.callback_data.startswith("cat:")]
    assert groups == list(range(len(WORKER_GROUPS)))


def test_section_button_answers_with_its_functions():
    msg, state = _Msg("💬 Активность"), _State()
    asyncio.run(menu.show_section(msg, state))

    text, markup = msg.answers[0]
    assert text.startswith("<b>💬 Активность</b>")
    assert "<b>🚪 Вступить в чат</b> — воркеры вступают" in text  # each item explained
    assert [b.text for b in _buttons(markup)][0] == "⚠️ 🚪 Вступить в чат"
    assert state.cleared


def test_worker_group_edits_the_message_in_place():
    callback = _Callback()
    asyncio.run(menu.show_worker_group(callback, CategoryCB(index=1), _State()))

    assert callback.message.answers == []  # no new message
    text, markup = callback.message.edits[0]
    assert text.startswith("<b>🔐 Безопасность</b>") and "облачный пароль" in text
    assert any("2FA" in b.text for b in _buttons(markup))
    assert callback.answered


def test_back_returns_to_workers_screen_in_place():
    callback = _Callback()
    pool = ns(count=lambda: 5)
    asyncio.run(accounts.back_to_workers(callback, _State(), pool))

    text, markup = callback.message.edits[0]
    assert "подключено: <b>5</b>" in text
    assert markup == screen_kb(WORKERS)


def test_workers_screen_with_no_workers_explains_how_to_add():
    msg = _Msg(WORKERS_BUTTON)
    asyncio.run(accounts.workers(msg, _State(), ns(count=lambda: 0)))

    text, markup = msg.answers[0]
    assert "подключено: <b>0</b>" in text and "tdata" in text
    assert markup == screen_kb(WORKERS)  # import stays reachable with zero workers


def test_every_function_is_explained_in_its_section():
    from bot.services.registry import BOT_FUNCTIONS, RISK_NOTE, SECTIONS, WORKER_GROUPS, section_text

    assert all(function.hint for function in BOT_FUNCTIONS)
    for category in (*SECTIONS, *WORKER_GROUPS):
        text = section_text(category)
        risky = any(f.risk == "risky" for f in BOT_FUNCTIONS if f.category == category)
        assert all(f.hint.replace("«", "").split()[0] in text.replace("«", "")
                   for f in BOT_FUNCTIONS if f.category == category)
        assert (RISK_NOTE in text) == risky


def test_workers_screen_explains_its_buttons():
    msg = _Msg(WORKERS_BUTTON)
    asyncio.run(accounts.workers(msg, _State(), ns(count=lambda: 3)))
    text = msg.answers[0][0]
    for button in SCREENS[WORKERS]:
        assert f"<b>{button.title}</b> — {button.hint}" in text
    assert "Задержка" not in text  # a setting: ⚙️ Настройки


def test_settings_screen_shows_the_values_and_explains_its_buttons():
    from bot.routers import settings as screen

    settings = ns(delay=[2, 5], profile_pause=[60, 180], autoreply_enabled=True, autoreply_text="")
    msg = _Msg(SETTINGS_BUTTON)
    asyncio.run(screen.settings_screen(msg, _State(), settings))
    text, markup = msg.answers[0]
    assert "задержка <b>2–5 с</b> · пауза профиля <b>60–180 с</b> · автоответ <b>выкл</b>" in text  # no text: off
    for button in SCREENS[SETTINGS]:
        assert f"<b>{button.title}</b> — {button.hint}" in text
    assert markup == screen_kb(SETTINGS)

    back = _Callback()
    asyncio.run(screen.back_to_settings(back, _State(), settings))
    assert back.message.answers == [] and back.message.edits[0][1] == screen_kb(SETTINGS)  # in place


def test_start_names_every_main_menu_button():
    from bot.services.registry import MAIN_MENU_HINTS

    text = menu.welcome("Ann", 2)
    assert list(MAIN_MENU_HINTS) == [PROJECTS_BUTTON, *SECTIONS, WORKERS, SETTINGS]
    for label, hint in MAIN_MENU_HINTS.items():
        assert f"<b>{label.split(' ', 1)[1]}</b> — {hint}" in text


def test_photo_folder_button_works_only_in_its_step():
    from bot.routers import profile
    from bot.states import ChangePhoto

    [handler] = [h for h in profile.router.callback_query.handlers if h.callback is profile.photo_folder]
    # an old message's button starts nothing
    assert ChangePhoto.photo in [f.callback for f in handler.filters]
