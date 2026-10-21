from itertools import groupby

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import FunctionCB, MenuAction, MenuCB, NavCB
from modules.registry import CARD, MAIN_MENU, SCREENS, by_category, children, in_project, risk_marker


def main_menu() -> ReplyKeyboardMarkup:
    """The main-menu screens two to a row: the projects and the settings on top, 📊 Задачи under them
    (registry SCREENS order)."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=title) for title in MAIN_MENU[i:i + 2]] for i in range(0, len(MAIN_MENU), 2)],
        resize_keyboard=True,
    )


SHORT = 16  # two labels this short fit side by side on a phone; a longer one gets its own row


def _widths(labels: list[str]) -> list[int]:
    """Row sizes for builder.adjust: two short labels in a row, a long one alone."""
    widths, i = [], 0
    while i < len(labels):
        pair = i + 1 < len(labels) and max(len(labels[i]), len(labels[i + 1])) <= SHORT
        widths.append(2 if pair else 1)
        i += widths[-1]
    return widths


def screen_kb(title: str, project: int = 0, hidden=(), titles: dict | None = None) -> InlineKeyboardMarkup:
    """A screen's buttons: its actions, its functions (each subheading on new rows) — two short ones in a
    row —, the screens opened from it two in a row; ⬅️ back to its parent on a row of its own. A project's
    screen's buttons carry its `project` (bot/callbacks.py); without the `hidden` actions, `titles` in
    place of some actions' own."""
    builder = InlineKeyboardBuilder()
    widths = []

    shown = [button for button in SCREENS[title].buttons if button.action not in hidden]
    actions = [(titles or {}).get(button.action, button.title) for button in shown]
    for button, label in zip(shown, actions):
        builder.button(text=label, callback_data=MenuCB(action=MenuAction(button.action), project=project))
    widths += _widths(actions)

    functions = by_category(title)
    for _, run in groupby(functions, key=lambda function: function.group):
        labels = []
        for function in run:
            labels.append(f"{risk_marker(function, functions)}{function.title}")
            builder.button(text=labels[-1], callback_data=FunctionCB(key=function.key, project=project))
        widths += _widths(labels)

    opened = children(title)
    for child in opened:
        builder.button(text=f"{child} ›", callback_data=NavCB(screen=child, project=project))
    widths += [2] * (len(opened) // 2) + [1] * (len(opened) % 2)

    if parent := SCREENS[title].parent:
        builder.button(text="⬅️ Назад", callback_data=NavCB(screen=parent, project=project if in_project(parent) else 0))
        widths.append(1)
    builder.adjust(*widths)
    return builder.as_markup()


def back_kb(screen: str, project: int = 0, fresh: bool = True) -> InlineKeyboardMarkup:
    """⬅️ to the screen a job or a form was started from, once it is over (its project's: `project`); `fresh`:
    the screen comes as a new message, this one (a report) stays."""
    label = "⬅️ К проекту" if screen == CARD else f"⬅️ {screen}"
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text=label, callback_data=NavCB(screen=screen, project=project, fresh=fresh).pack())]])
