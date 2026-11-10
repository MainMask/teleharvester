import html
from collections.abc import Awaitable, Callable

from bot.keyboards.menu import screen_kb
from modules import projects
from modules.registry import (CARD, PROJECTS, RISK_NOTE, RISKY, SCREENS, all_risky, by_category, children, entries,
                              in_project, trail)

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


def head(title: str) -> str:
    """A title in a message (HTML) — a screen's, a function's, a button's, a choice's —, its emoji too: bold, as
    the screens show theirs (title_line, screen_text). A form's prompt, question or confirmation starts with its own."""
    return f"<b>{html.escape(title)}</b>"


def title_line(title: str, name: str = "") -> str:
    """A screen's title (HTML): a project's (its `name`) as the path from its card — 📁 GreenBase › 🤖 Аккаунты ›
    👤 Профиль —; ⚠️ once on it when every button there is risky (registry.all_risky)."""
    risky = " ⚠️" if all_risky(title) else ""
    if not name:
        return head(title) + risky
    below = [part for part in trail(title) if part not in (PROJECTS, CARD)]
    return " › ".join([f"📁 {head(name)}", *(head(part) for part in below)]) + risky


async def show(message, title: str, edit: bool, header: str = "", project: int = 0, hidden=(),
               titles: dict | None = None):
    """A screen as the registry has it: its message (`header` on top, by default its title_line) and its
    buttons, acting on `project` (screen_kb)."""
    header = header or title_line(title, project_name(project) if project else "")
    await send(message, edit)(screen_text(title, header, hidden, titles), parse_mode="HTML",
                              reply_markup=screen_kb(title, project, hidden, titles))


def _line(title: str, hint: str, marker: str = "") -> str:
    return f"{head(title)}{marker} — {html.escape(hint)}"


def screen_text(title: str, header: str = "", hidden=(), titles: dict | None = None, own=()) -> str:
    """A screen's message (HTML): `header` (its title_line by default), then each of its buttons with
    what it does — its actions (`titles` in place of their own) and its functions, without the `hidden`, under
    their subheadings (registry.entries), its view's `own` buttons ((title, hint), as screen_kb's `own`), the
    screens opened from it; a blank line before each subheading and after the last subheading's buttons."""
    blocks, block, group = [[header or title_line(title)]], [], ""
    for entry in entries(title, hidden, titles):
        if entry.group != group:
            group = entry.group
            blocks, block = blocks + [block], [f"<i>{html.escape(group)}</i>"] if group else []
        block.append(_line(entry.label, entry.hint, entry.marker))
    if group:
        blocks, block = blocks + [block], []
    block += [_line(label, hint) for label, hint in own]
    block += [_line(child, SCREENS[child].hint) for child in children(title)]
    blocks.append(block)
    if any(function.risk == RISKY for function in by_category(title)):
        blocks.append([f"<i>{RISK_NOTE}</i>"])
    return "\n\n".join("\n".join(block) for block in blocks if block)
