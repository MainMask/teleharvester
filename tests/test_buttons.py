"""The menu, as the registry describes it: every button starts with an emoji that says what it is,
every function and hub-screen button is described where it is shown, and messages point to the
menu through menu_path, never a path typed by hand."""

import ast
import glob
import types
import unicodedata

from bot.callbacks import MenuAction
from bot.keyboards.common import yes_no_kb
from bot.keyboards.menu import main_menu, screen_kb
from bot.routers import broadcasts
from modules.analysis_tools import TOOLS as ANALYSIS
from modules.registry import (
    ACCOUNTS, BOT_FUNCTIONS, BOT_FUNCTIONS_BY_KEY, CLI_EXTRAS, MAIN_MENU, NOT_MENU_ITEMS, PROJECT_CARD, PROJECT_MANAGE,
    SCREENS, children, menu_path,
)
from bot.services.screens import screen_text


def _starts_with_emoji(text: str) -> bool:
    return unicodedata.category(text.removeprefix("⚠️ ")[0]).startswith("S")  # a symbol, not a letter


def _texts(markup):
    rows = markup.inline_keyboard if hasattr(markup, "inline_keyboard") else markup.keyboard
    return [button.text for row in rows for button in row]


def test_every_function_has_an_emoji_and_a_description():
    for function in BOT_FUNCTIONS + CLI_EXTRAS:
        assert _starts_with_emoji(function.title), function.title
        assert function.hint.strip(), f"{function.title}: no description"


def test_every_menu_button_starts_with_an_emoji():
    from bot.keyboards.common import cancel_kb
    from bot.routers.login import add_kb
    from bot.states import SetDelay

    texts = _texts(main_menu()) + _texts(yes_no_kb("x")) + _texts(cancel_kb(SetDelay.input)) + _texts(add_kb())
    for screen in SCREENS:
        texts += _texts(screen_kb(screen))
    texts += [label for label, _ in broadcasts.MODE_OPTIONS + broadcasts.INSTANT_MODES + broadcasts.MMODE_OPTIONS]
    texts += [label for label, *_ in ANALYSIS.values()]
    from functions.report_user import ReportUserFunc
    texts += [label for label, _ in ReportUserFunc(types.SimpleNamespace(sessions=[]), None).reasons]
    assert [text for text in texts if not _starts_with_emoji(text)] == []


def test_every_screen_is_described_and_the_main_menu_is_the_screens_without_a_parent():
    assert list(MAIN_MENU) == _texts(main_menu())
    assert all(screen.hint.strip() for screen in SCREENS.values())


def test_every_screen_button_is_described_on_its_screen_and_does_something():
    from modules.registry import PROJECTS

    actions = {action.value for action in MenuAction}
    for title, screen in SCREENS.items():
        if title == PROJECTS:  # its own list and cards (bot/routers/projects.py)
            continue
        text = screen_text(title)
        for button in screen.buttons:
            assert button.hint.strip() and f"<b>{button.title}</b> — {button.hint}" in text
            assert button.action in actions, button.title
        for child in children(title):
            assert f"<b>{child}</b> — {SCREENS[child].hint}" in text


def test_the_cli_accounts_items_are_the_bot_s_buttons():
    titles = {button.title for button in SCREENS[ACCOUNTS].buttons}
    assert {f.title for f in CLI_EXTRAS if f.category == ACCOUNTS} == titles


def test_every_analysis_tool_is_described():
    assert [tool for tool, (_, hint, *_) in ANALYSIS.items() if not hint.strip()] == []


def test_menu_path_finds_functions_and_screen_buttons():
    # all the work is a project's: on its card (📁 Проект), or its screens
    assert menu_path("status") == "📁 Проекты → 📁 Проект → 🤖 Аккаунты → 🛡 Проверка статуса (SpamBot)"
    assert menu_path("2fa") == "📁 Проекты → 📁 Проект → 🤖 Аккаунты → 🛠 Обслуживание → 🔒 Установить 2FA"
    assert menu_path("invite") == "📁 Проекты → 📁 Проект → 💬 Активность → 🤝 Инвайтинг"
    assert menu_path("autoreply") == "⚙️ Настройки → 📬 Автоответ"
    assert menu_path("members") == "📁 Проекты → 📁 Проект → 👥 Участники чата"
    assert "projects" not in BOT_FUNCTIONS_BY_KEY  # 📁 Проекты: a main-menu button; the CLI's item (CLI_EXTRAS)
    assert menu_path("add") == "📁 Проекты → 📁 Проект → 🤖 Аккаунты → ➕ Добавить"
    actions = {b.action for screen in SCREENS.values() for b in screen.buttons}
    assert not actions & set(BOT_FUNCTIONS_BY_KEY)  # one key, one place


def test_the_card_s_functions_are_its_own_and_its_screen_says_what_each_button_does():
    on_card = {BOT_FUNCTIONS_BY_KEY[b.function].classname for row in PROJECT_CARD for b in row if b.function}
    # not menu items of their own: run from a card or ➕ Добавить
    assert set(NOT_MENU_ITEMS) == on_card | {"ScrapeFunc", "TdataImportFunc", "LoginCodesFunc"}
    assert all(b.hint for row in PROJECT_MANAGE for b in row)


def test_no_message_types_a_menu_path_by_hand():
    """A path typed by hand outlives a moved or renamed button: messages use menu_path."""
    hubs = tuple(f"{label} →" for label in MAIN_MENU)
    typed = []
    for path in glob.glob("bot/**/*.py", recursive=True) + glob.glob("functions/**/*.py", recursive=True):
        if path.endswith("registry.py"):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                      if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                      and node.body and isinstance(node.body[0], ast.Expr)
                      and isinstance(node.body[0].value, ast.Constant)}
        typed += [f"{path}:{node.lineno}" for node in ast.walk(tree)
                  if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
                  and any(hub in node.value for hub in hubs)]
    assert typed == []


class _Screen:
    """A message the bot answers to, or a callback's message: what it answered."""

    def __init__(self, text=None):
        self.text, self.answers = text, []
        self.chat = types.SimpleNamespace(id=1)
        self.message = self  # a callback's .message

    async def answer(self, text=None, **kwargs):
        if text is not None:
            self.answers.append(text)


class _Clean:
    async def clear(self):
        pass

    async def update_data(self, **kwargs):
        pass


def test_every_screen_of_the_main_menu_starts_with_an_emoji(monkeypatch, tmp_path):
    import asyncio

    from bot.routers import menu, scraping
    from modules.registry import ACTIVITY, PROJECTS, SETTINGS
    from modules import projects

    deps = dict(pool=types.SimpleNamespace(count=lambda: 1), settings=types.SimpleNamespace(
        delay=[2, 5], profile_pause=[60, 180], autoreply_enabled=False, autoreply_text=""),
        workspace=types.SimpleNamespace())  # a project opened: its screens

    def opened(title):
        msg = _Screen(title)
        asyncio.run(menu.open_from_menu(msg, _Clean(), **deps))
        return msg.answers

    texts = opened(ACTIVITY) + opened(PROJECTS)  # the projects' list says one thing when it's empty,
    projects.add("Green", ["@a"])
    texts += opened(PROJECTS) + opened(ACCOUNTS) + opened(SETTINGS)  # another when not
    callback = _Screen()
    # a screen of tools too
    asyncio.run(scraping.analysis.analysis_project(callback, _Clean(), types.SimpleNamespace(name="P")))
    texts += callback.answers

    assert len(texts) == 6
    assert [text[:40] for text in texts if not _starts_with_emoji(text.removeprefix("<b>"))] == []


def test_a_long_choice_is_paged_under_telegram_s_100_buttons():
    from bot.callbacks import ChoiceCB
    from bot.keyboards.common import PAGE_SIZE, paged_kb

    options = [(f"🤖 w{i}", str(i)) for i in range(130)]
    rows = paged_kb("x", options, 0, ".t").inline_keyboard
    assert len(rows) == PAGE_SIZE + 1 and [b.text for b in rows[-1]] == ["⬅️", "📄 стр 1/7", "➡️"]
    assert [ChoiceCB.unpack(b.callback_data).value for b in rows[-1]] == ["pg.6.t", "pg.0.t", "pg.1.t"]  # round
    last = paged_kb("x", options, 6).inline_keyboard
    assert [b.text for row in last[:-1] for b in row] == [f"🤖 w{i}" for i in range(120, 130)]
    assert len(paged_kb("x", options[:PAGE_SIZE], 0).inline_keyboard) == PAGE_SIZE  # one page: no ⬅️ · ➡️
