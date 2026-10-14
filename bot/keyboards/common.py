from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import ChoiceCB


def choice_kb(scope: str, options: list[tuple[str, str]], widths: tuple[int, ...] = (1,)) -> InlineKeyboardMarkup:
    """An inline keyboard of (label, value) choices, all tagged with `scope`; `widths`: buttons per row
    (builder.adjust: the last one repeats)."""
    builder = InlineKeyboardBuilder()

    for label, value in options:
        builder.button(text=label, callback_data=ChoiceCB(scope=scope, value=value))

    builder.adjust(*widths)
    return builder.as_markup()


PAGE_SIZE = 20  # choices per page of paged_kb: Telegram takes at most 100 buttons under a message


def paged_kb(scope: str, options: list[tuple[str, str]], page: int, suffix: str = "") -> InlineKeyboardMarkup:
    """choice_kb of one page of `options`, one per row; with more than a page, ⬅️ · 📄 · ➡️ under it, their
    values "pg.<page>" + `suffix` (⬅️/➡️ the next page round, 📄 this one): the handler redraws that page."""
    pages = max(1, -(-len(options) // PAGE_SIZE))
    page %= pages
    shown = options[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    nav = [("⬅️", f"pg.{(page - 1) % pages}{suffix}"), (f"📄 стр {page + 1}/{pages}", f"pg.{page}{suffix}"),
           ("➡️", f"pg.{(page + 1) % pages}{suffix}")] if pages > 1 else []
    return choice_kb(scope, shown + nav, widths=(*[1] * len(shown), 3))


def cancel_kb(step, markup: InlineKeyboardMarkup | None = None) -> InlineKeyboardMarkup:
    """✖️ Отмена under a step's prompt (a State, or its name, waiting for text or a file): as
    /cancel, while the chat is still at that step (callback data can't hold ':', so the state's
    name has '.'). `markup`: the prompt's own buttons, the cancel goes last."""
    name = getattr(step, "state", step)
    cancel = choice_kb("cancel", [("✖️ Отмена", name.replace(":", "."))])
    if markup is None:
        return cancel
    return InlineKeyboardMarkup(inline_keyboard=[*markup.inline_keyboard, *cancel.inline_keyboard])


def yes_no_kb(scope: str) -> InlineKeyboardMarkup:
    return choice_kb(scope, [("✅ Да", "yes"), ("❌ Нет", "no")])


def stop_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📊 Прогресс", callback_data=ChoiceCB(scope="job_progress", value="show"))
    builder.button(text="⏹ Стоп", callback_data=ChoiceCB(scope="job_stop", value="stop"))
    return builder.as_markup()


def scrape_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📊 Прогресс", callback_data=ChoiceCB(scope="job_progress", value="scrape"))
    builder.button(text="⏹ Стоп", callback_data=ChoiceCB(scope="scrape_stop", value="stop"))
    return builder.as_markup()
