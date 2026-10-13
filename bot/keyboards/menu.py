from aiogram.types import (
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import CategoryCB, FunctionCB, MenuAction, MenuCB
from bot.services.registry import PROJECTS, RISKY, SCREENS, SECTIONS, SETTINGS, WORKER_GROUPS, WORKERS, by_category

PROJECTS_BUTTON = PROJECTS
WORKERS_BUTTON = WORKERS
SETTINGS_BUTTON = SETTINGS


def main_menu() -> ReplyKeyboardMarkup:
    """The projects on top, the sections of functions in a row, the two hub screens under them."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=PROJECTS_BUTTON)],
            [KeyboardButton(text=section) for section in SECTIONS],
            [KeyboardButton(text=WORKERS_BUTTON), KeyboardButton(text=SETTINGS_BUTTON)],
        ],
        resize_keyboard=True,
    )


def functions_kb(category: str, back: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    for function in by_category(category):
        marker = "⚠️ " if function.risk == RISKY else ""
        builder.button(
            text=f"{marker}{function.title}",
            callback_data=FunctionCB(key=function.key),
        )

    if back:
        builder.button(text="⬅️ Назад", callback_data=MenuCB(action=MenuAction.WORKERS))
    builder.adjust(1)
    return builder.as_markup()


def screen_kb(screen: str) -> InlineKeyboardMarkup:
    """A hub screen's buttons (registry SCREENS): an action, or a group of functions to open."""
    builder = InlineKeyboardBuilder()
    for button in SCREENS[screen]:
        callback = (MenuCB(action=MenuAction(button.action)) if button.action
                    else CategoryCB(index=WORKER_GROUPS.index(button.title)))
        builder.button(text=button.title, callback_data=callback)
    builder.adjust(1)
    return builder.as_markup()
