from itertools import groupby

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import FunctionCB, MenuAction, MenuCB, NavCB
from modules.registry import CARD, MAIN_MENU, SCREENS, children, entries, in_project


def main_menu() -> ReplyKeyboardMarkup:
    """The main-menu screens in one row, in registry SCREENS order."""
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=title) for title in MAIN_MENU]], resize_keyboard=True)


REFRESH = "🔄 Обновить"  # a live screen anew in its place (registry Screen.refresh)
SHORT = 16  # two labels this short fit side by side on a phone; a longer one gets its own row


def _widths(labels: list[str]) -> list[int]:
    """Row sizes for builder.adjust: two short labels in a row, a long one alone."""
    widths, i = [], 0
    while i < len(labels):
        pair = i + 1 < len(labels) and max(len(labels[i]), len(labels[i + 1])) <= SHORT
        widths.append(2 if pair else 1)
        i += widths[-1]
    return widths


def screen_kb(title: str, project: int = 0, hidden=(), titles: dict | None = None,
              own: list[list[InlineKeyboardButton]] = ()) -> InlineKeyboardMarkup:
    """A screen's buttons: its actions, then its functions, each on new rows — but those under one subheading
    together (registry.entries; their ⚠️ in the screen's text only, screens.screen_text) — two short ones in a
    row —, its view's `own` rows (registry Screen.own_buttons), the screens opened from it two in a row; on the
    last row 🔄 Обновить (registry Screen.refresh) and ⬅️ back to its parent. A project's screen's buttons carry
    its `project` (bot/callbacks.py); without the `hidden` actions and functions, `titles` in place of some
    actions' own."""
    builder = InlineKeyboardBuilder()
    widths = []

    for _, run in groupby(entries(title, hidden, titles), key=lambda entry: entry.group or entry.function):
        labels = []
        for entry in run:
            labels.append(entry.label)
            builder.button(text=entry.label, callback_data=FunctionCB(key=entry.key, project=project)
                           if entry.function else MenuCB(action=MenuAction(entry.key), project=project))
        widths += _widths(labels)

    for row in own:
        for item in row:
            builder.add(item)
        widths.append(len(row))

    opened = children(title)
    for child in opened:
        builder.button(text=f"{child} ›", callback_data=NavCB(screen=child, project=project))
    widths += [2] * (len(opened) // 2) + [1] * (len(opened) % 2)

    last = [nav_button(REFRESH, title, project)] if SCREENS[title].refresh else []
    if SCREENS[title].parent:
        last.append(back_button(title, project))
    for item in last:
        builder.add(item)
    if last:
        widths.append(len(last))
    builder.adjust(*widths)
    return builder.as_markup()


def back_button(title: str, project: int = 0) -> InlineKeyboardButton:
    """⬅️ Назад of a screen: to its parent (registry SCREENS), its project's if the parent is a project's."""
    parent = SCREENS[title].parent
    return nav_button("⬅️ Назад", parent, project if in_project(parent) else 0)


def nav_button(text: str, screen: str, project: int = 0, fresh: bool = False) -> InlineKeyboardButton:
    """A button opening a screen (its project's: `project`); `fresh`: as a new message, this one stays."""
    return InlineKeyboardButton(text=text, callback_data=NavCB(screen=screen, project=project, fresh=fresh).pack())


def back_kb(screen: str, project: int = 0, fresh: bool = True) -> InlineKeyboardMarkup:
    """⬅️ to the screen a job or a form was started from, once it is over (its project's: `project`); `fresh`:
    the screen comes as a new message, this one (a report) stays."""
    label = "⬅️ К проекту" if screen == CARD else f"⬅️ {screen}"
    return InlineKeyboardMarkup(inline_keyboard=[[nav_button(label, screen, project, fresh)]])
