from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import CategoryCB, FunctionCB, MenuAction, MenuCB
from bot.services.registry import RISKY, by_category, categories


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Функции"), KeyboardButton(text="👥 Аккаунты")],
        ],
        resize_keyboard=True,
    )


def categories_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    for index, name in enumerate(categories()):
        builder.button(text=name, callback_data=CategoryCB(index=index))

    builder.adjust(1)
    return builder.as_markup()


def functions_kb(category: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    for function in by_category(category):
        marker = "⚠️ " if function.risk == RISKY else ""
        builder.button(
            text=f"{marker}{function.title}",
            callback_data=FunctionCB(key=function.key),
        )

    builder.button(text="⬅️ Категории", callback_data=MenuCB(action=MenuAction.CATEGORIES))
    builder.adjust(1)
    return builder.as_markup()


def accounts_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🌐 Настроить прокси", callback_data=MenuCB(action=MenuAction.PROXY).pack())],
            [InlineKeyboardButton(text="📥 Загрузить tdata (ZIP)", callback_data=MenuCB(action=MenuAction.TDATA).pack())],
        ]
    )
