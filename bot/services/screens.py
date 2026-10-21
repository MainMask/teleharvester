import html
from collections.abc import Awaitable, Callable

from bot.keyboards.menu import screen_kb
from modules import projects
from modules.registry import (CARD, PROJECTS, RISK_NOTE, RISKY, SCREENS, by_category, children, in_project,
                              risk_marker, trail)

# How the bot shows the registry's screens (modules/registry.py): their messages, and how each
# is opened. bot/routers/menu.py opens every screen through open_screen — from the main menu as a new
# message, by a button in that message's place — so a screen needs no handler of its own: one with
# live values, or of a kind of its own, registers its view (view) from its router.

# (message, state, edit, **deps): `edit` — in the message's place, not a new one; `deps` — what
# aiogram gave the handler (pool, settings, the job slots…), each view takes the ones it needs by name
View = Callable[..., Awaitable[None]]
VIEWS: dict[str, View] = {}


def view(title: str):
    """Register the decorated function as the screen's view (instead of show)."""
    def register(function: View) -> View:
        VIEWS[title] = function
        return function
    return register


async def open_screen(title: str, message, state, edit: bool, deps: dict):
    """The screen by its view, or as the registry has it (a project's: its project's, ProjectMiddleware's)."""
    if title in VIEWS:
        await VIEWS[title](message, state, edit, **deps)
    else:
        project = deps["workspace"].project_id if in_project(title) else 0
        await show(message, title, edit, project=project)


def send(message, edit: bool):
    """How a screen is sent: `edit` — in the message's place (a button's), else as a new message."""
    return message.edit_text if edit else message.answer


def project_name(project_id: int) -> str:
    """The project's name; "" if it is gone or its file can't be read (the screen then goes without it)."""
    try:
        return next((project.name for project in projects.load() if project.id == project_id), "")
    except ValueError:
        return ""


def title_line(title: str, name: str = "") -> str:
    """A screen's title (HTML): a project's (its `name`) as the path from its card — 📁 GreenBase › 🤖 Аккаунты ›
    👤 Профиль —; ⚠️ once on it when every function there is risky (registry.risk_marker)."""
    functions = by_category(title)
    risky = " ⚠️" if functions and all(function.risk == RISKY for function in functions) else ""
    if not name:
        return f"<b>{html.escape(title)}</b>{risky}"
    below = [part for part in trail(title) if part not in (PROJECTS, CARD)]
    return " › ".join([f"📁 <b>{html.escape(name)}</b>", *(f"<b>{html.escape(part)}</b>" for part in below)]) + risky


async def show(message, title: str, edit: bool, header: str = "", project: int = 0, hidden=(),
               titles: dict | None = None):
    """A screen as the registry has it: its message (`header` on top, by default its title_line) and its
    buttons, acting on `project` (screen_kb)."""
    header = header or title_line(title, project_name(project) if project else "")
    await send(message, edit)(screen_text(title, header, hidden, titles), parse_mode="HTML",
                              reply_markup=screen_kb(title, project, hidden, titles))


def _line(title: str, hint: str) -> str:
    return f"<b>{html.escape(title)}</b> — {html.escape(hint)}"


def screen_text(title: str, header: str = "", hidden=(), titles: dict | None = None) -> str:
    """A screen's message (HTML): `header` (its title_line by default), then each of its buttons with
    what it does — its actions (without the `hidden`, `titles` in place of their own), its functions under
    their subheadings, the screens opened from it; a blank line before each subheading and after its
    functions."""
    functions = by_category(title)
    blocks = [[header or title_line(title)]]
    block = [_line((titles or {}).get(button.action, button.title), button.hint)
             for button in SCREENS[title].buttons if button.action not in hidden]
    group = ""
    for function in functions:
        if function.group != group:
            group = function.group
            blocks, block = blocks + [block], [f"<i>{html.escape(group)}</i>"]
        block.append(f"{risk_marker(function, functions)}{_line(function.title, function.hint)}")
    if group:
        blocks, block = blocks + [block], []
    block += [_line(child, SCREENS[child].hint) for child in children(title)]
    blocks.append(block)
    if any(function.risk == RISKY for function in functions):
        blocks.append([f"<i>{RISK_NOTE}</i>"])
    return "\n\n".join("\n".join(block) for block in blocks if block)
