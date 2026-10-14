from itertools import groupby

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import FunctionCB, MenuAction, MenuCB, NavCB
from bot.keyboards.common import choice_kb
from bot.services.registry import MAIN_MENU, SCREENS, button_title, by_category, children, risk_marker


def main_menu() -> ReplyKeyboardMarkup:
    """The main-menu screens two to a row: the work — the projects and 💬 Активность — on top, the
    accounts and the settings under it, 📊 Задачи last (registry SCREENS order)."""
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


def screen_button(title: str) -> InlineKeyboardButton:
    """The button that opens a screen (registry SCREENS), marked ›."""
    return InlineKeyboardButton(text=f"{title} ›", callback_data=NavCB(screen=title).pack())


def screen_kb(title: str) -> InlineKeyboardMarkup:
    """A screen's buttons: its actions, its functions (each subheading on new rows), the screens
    opened from it — two short ones in a row; ⬅️ back to its parent on a row of its own."""
    builder = InlineKeyboardBuilder()
    widths = []

    actions = [button_title(button) for button in SCREENS[title].buttons]
    for button, label in zip(SCREENS[title].buttons, actions):
        builder.button(text=label, callback_data=MenuCB(action=MenuAction(button.action)))
    widths += _widths(actions)

    functions = by_category(title)
    for _, run in groupby(functions, key=lambda function: function.group):
        labels = []
        for function in run:
            labels.append(f"{risk_marker(function, functions)}{function.title}")
            builder.button(text=labels[-1], callback_data=FunctionCB(key=function.key))
        widths += _widths(labels)

    opened = [screen_button(child) for child in children(title)]
    for button in opened:
        builder.add(button)
    widths += _widths([button.text for button in opened])

    if parent := SCREENS[title].parent:
        builder.button(text="⬅️ Назад", callback_data=NavCB(screen=parent))
        widths.append(1)
    builder.adjust(*widths)
    return builder.as_markup()


def card_kb(rows: list[list[tuple[str, str]]], token: str) -> InlineKeyboardMarkup:
    """A card's (title, action) rows (registry.card_rows, then its ⬅️), each button carrying the card's
    token: an older card's must not act on the one opened since."""
    return choice_kb("prj_act", [(label, f"{action}.{token}") for row in rows for label, action in row],
                     widths=tuple(len(row) for row in rows))
