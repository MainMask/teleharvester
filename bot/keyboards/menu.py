from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import CategoryCB, FunctionCB, MenuAction, MenuCB
from bot.services.registry import RISKY, SECTIONS, WORKER_GROUPS, by_category

WORKERS_BUTTON = "🤖 Воркеры"


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=SECTIONS[0]), KeyboardButton(text=SECTIONS[1])],
            [KeyboardButton(text=SECTIONS[2]), KeyboardButton(text=WORKERS_BUTTON)],
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


def workers_kb() -> InlineKeyboardMarkup:
    def button(text, callback_data):
        return InlineKeyboardButton(text=text, callback_data=callback_data.pack())

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [button("📋 Список аккаунтов", MenuCB(action=MenuAction.LIST))],
            *[[button(group, CategoryCB(index=index))] for index, group in enumerate(WORKER_GROUPS)],
            [button("🌐 Прокси", MenuCB(action=MenuAction.PROXY)),
             button("⏱ Задержка", MenuCB(action=MenuAction.DELAY))],
            [button("📥 Загрузить tdata", MenuCB(action=MenuAction.TDATA))],
        ]
    )
