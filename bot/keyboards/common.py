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


def marks_kb(scope: str, labels: list[str], picked: list[int], page: int, actions: list[tuple[str, str]],
             widths: tuple[int, ...]) -> InlineKeyboardMarkup:
    """A list to tick, a page of it: each label ✅ or ⬜ (`picked`, by index), its value its index; with more
    than a page, ⬅️ · 📄 · ➡️ ("prev", "page", "next") under it; then the `actions` (title, value), in rows of
    `widths`. The handler: marks_step."""
    pages = max(1, -(-len(labels) // PAGE_SIZE))
    shown = range(page * PAGE_SIZE, min((page + 1) * PAGE_SIZE, len(labels)))
    nav = [("⬅️", "prev"), (f"📄 стр {page + 1}/{pages}", "page"), ("➡️", "next")] if pages > 1 else []
    options = [(f"{'✅' if index in picked else '⬜'} {labels[index]}", str(index)) for index in shown]
    return choice_kb(scope, options + nav + actions, widths=(*[1] * len(options), *([3] if nav else []), *widths))


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


def stop_kb(slot: int) -> InlineKeyboardMarkup:
    """📊 and ⏹ of the job in the slot (JobManager.key)."""
    builder = InlineKeyboardBuilder()
    builder.button(text="📊 Прогресс", callback_data=ChoiceCB(scope="job_progress", value=str(slot)))
    builder.button(text="⏹ Стоп", callback_data=ChoiceCB(scope="job_stop", value=str(slot)))
    return builder.as_markup()


def scrape_kb(slot: int) -> InlineKeyboardMarkup:
    """📊 and ⏹ of a scraper's job (a scrape, members, a verify): its ⏹ keeps what is collected."""
    builder = InlineKeyboardBuilder()
    builder.button(text="📊 Прогресс", callback_data=ChoiceCB(scope="job_progress", value=str(slot)))
    builder.button(text="⏹ Стоп", callback_data=ChoiceCB(scope="scrape_stop", value=str(slot)))
    return builder.as_markup()
