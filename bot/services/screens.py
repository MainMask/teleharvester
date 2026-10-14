import html
from collections.abc import Awaitable, Callable

from bot.keyboards.menu import screen_kb
from bot.services.registry import RISK_NOTE, RISKY, SCREENS, by_category, card_rows, children, risk_marker

# How the bot shows the registry's screens (bot/services/registry.py): their messages, and how each
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
    """The screen by its view, or as the registry has it."""
    if title in VIEWS:
        await VIEWS[title](message, state, edit, **deps)
    else:
        await show(message, title, edit)


async def show(message, title: str, edit: bool, header: str = ""):
    """A screen as the registry has it: its message (`header` on top) and its buttons."""
    send = message.edit_text if edit else message.answer
    await send(screen_text(title, header), parse_mode="HTML", reply_markup=screen_kb(title))


def _line(title: str, hint: str) -> str:
    return f"<b>{html.escape(title)}</b> — {html.escape(hint)}"


def screen_text(title: str, header: str = "") -> str:
    """A screen's message (HTML): `header` (its bold title by default), then each of its buttons with
    what it does — its actions, its functions under their subheadings, the screens opened from it;
    a blank line before each subheading and after its functions."""
    functions = by_category(title)
    all_risky = bool(functions) and all(function.risk == RISKY for function in functions)
    blocks = [[header or f"<b>{html.escape(title)}</b>" + (" ⚠️" if all_risky else "")]]
    block = [_line(button.title, button.hint) for button in SCREENS[title].buttons]
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


def card_text(rows, hidden=(), channels: bool = True) -> str:
    """Each shown button of a card's screen with what it does (HTML), as on the other screens."""
    hints = {button.action: button.hint for row in rows for button in row}
    return "\n".join(_line(title, hints[action])
                     for row in card_rows(rows, hidden, channels) for title, action in row if hints[action])
