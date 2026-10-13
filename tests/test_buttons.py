"""The menu, as the registry describes it: every button starts with an emoji that says what it is,
every function and hub-screen button is described where it is shown, and messages point to the
menu through menu_path, never a path typed by hand."""

import ast
import glob
import types
import unicodedata

from bot.callbacks import MenuAction
from bot.keyboards.common import yes_no_kb
from bot.keyboards.menu import functions_kb, main_menu, screen_kb
from bot.routers import broadcasts
from bot.routers.scraping import ANALYSIS
from bot.services.registry import (
    BOT_FUNCTIONS, BOT_FUNCTIONS_BY_KEY, CLI_EXTRAS, MAIN_MENU_HINTS, SCREENS, SECTIONS, SETTINGS, WORKER_GROUPS,
    WORKERS, menu_path, screen_text,
)


def _starts_with_emoji(text: str) -> bool:
    return unicodedata.category(text.removeprefix("⚠️ ")[0]).startswith("S")  # a symbol, not a letter


def _texts(markup):
    rows = getattr(markup, "inline_keyboard", None) or markup.keyboard
    return [button.text for row in rows for button in row]


def test_every_function_has_an_emoji_and_a_description():
    for function in BOT_FUNCTIONS + CLI_EXTRAS:
        assert _starts_with_emoji(function.title), function.title
        assert function.hint.strip(), f"{function.title}: no description"


def test_every_menu_button_starts_with_an_emoji():
    texts = _texts(main_menu()) + _texts(yes_no_kb("x"))
    for screen in SCREENS:
        texts += _texts(screen_kb(screen))
    for section in (*SECTIONS, *WORKER_GROUPS):
        texts += _texts(functions_kb(section, back=section in WORKER_GROUPS))
    texts += [label for label, _ in broadcasts.MODE_OPTIONS + broadcasts.INSTANT_MODES + broadcasts.MMODE_OPTIONS]
    texts += [label for label, *_ in ANALYSIS.values()]
    from functions.report_user import ReportUserFunc
    texts += [label for label, _ in ReportUserFunc(types.SimpleNamespace(sessions=[]), None).reasons]
    assert [text for text in texts if not _starts_with_emoji(text)] == []


def test_every_main_menu_button_is_described_and_opens_something():
    assert list(MAIN_MENU_HINTS) == _texts(main_menu())
    assert all(hint.strip() for hint in MAIN_MENU_HINTS.values())
    from bot.services.registry import PROJECTS

    assert set(MAIN_MENU_HINTS) == {PROJECTS, *SECTIONS, *SCREENS}  # the projects, a section or a hub screen


def test_every_screen_button_is_described_on_its_screen_and_does_something():
    actions = {action.value for action in MenuAction}
    for screen, buttons in SCREENS.items():
        text = screen_text(screen)
        for button in buttons:
            assert button.hint.strip() and f"<b>{button.title}</b> — {button.hint}" in text
            assert button.action in actions or button.title in WORKER_GROUPS, button.title
    # every group of functions is on the workers screen; settings hold no function groups
    assert {b.title for b in SCREENS[WORKERS] if not b.action} == set(WORKER_GROUPS)
    assert all(b.action for b in SCREENS[SETTINGS])


def test_the_cli_workers_items_are_the_bot_s_buttons():
    titles = {button.title for button in SCREENS[WORKERS]}
    assert [f.title for f in CLI_EXTRAS if f.category == WORKERS and f.title not in titles] == ["🗑 Убрать личный аккаунт"]  # CLI only


def test_every_analysis_tool_is_described():
    assert [tool for tool, (_, hint, *_) in ANALYSIS.items() if not hint.strip()] == []


def test_menu_path_finds_functions_and_screen_buttons():
    assert menu_path("status") == "🤖 Воркеры → 🩺 Проверка и обслуживание → 🛡 Проверка статуса (SpamBot)"
    assert menu_path("invite") == "💬 Активность → 🤝 Инвайтинг из чата"
    assert menu_path("autoreply") == "⚙️ Настройки → 📬 Автоответ"
    assert menu_path("phone") == "🤖 Воркеры → 📲 Добавить по номеру"
    actions = {b.action for buttons in SCREENS.values() for b in buttons if b.action}
    assert not actions & set(BOT_FUNCTIONS_BY_KEY)  # one key, one place


def test_no_message_types_a_menu_path_by_hand():
    """A path typed by hand outlives a moved or renamed button: messages use menu_path."""
    hubs = tuple(f"{label} →" for label in MAIN_MENU_HINTS)
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

    from bot.routers import accounts, menu, projects as projects_menu, scraping, settings as settings_menu
    from modules import projects

    texts = []
    for section in SECTIONS:
        msg = _Screen(section)
        asyncio.run(menu.show_section(msg, _Clean()))
        texts += msg.answers
    for has_projects in (False, True):  # the list says one thing when it's empty, another when not
        if has_projects:
            projects.add("Green", ["@a"])
        msg = _Screen()
        asyncio.run(projects_menu.projects_menu(msg, _Clean()))
        texts += msg.answers
    msg = _Screen()
    asyncio.run(accounts.workers(msg, _Clean(), types.SimpleNamespace(count=lambda: 1)))
    texts += msg.answers
    msg = _Screen()
    asyncio.run(settings_menu.settings_screen(msg, _Clean(), types.SimpleNamespace(
        delay=[2, 5], profile_pause=[60, 180], autoreply_enabled=False, autoreply_text="")))
    texts += msg.answers
    callback = _Screen()
    asyncio.run(scraping.analysis_start(callback, _Clean()))  # the analysis is a screen of tools too
    texts += callback.answers

    assert len(texts) == len(SECTIONS) + 5
    assert [text[:40] for text in texts if not _starts_with_emoji(text.removeprefix("<b>"))] == []
