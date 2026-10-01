from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import ChoiceCB


def choice_kb(scope: str, options: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """An inline keyboard of (label, value) choices, all tagged with `scope`."""
    builder = InlineKeyboardBuilder()

    for label, value in options:
        builder.button(text=label, callback_data=ChoiceCB(scope=scope, value=value))

    builder.adjust(1)
    return builder.as_markup()


def yes_no_kb(scope: str) -> InlineKeyboardMarkup:
    return choice_kb(scope, [("Да", "yes"), ("Нет", "no")])


def stop_kb() -> InlineKeyboardMarkup:
    return choice_kb("job_stop", [("⏹ Стоп", "stop")])
