"""The auto-reply screen (🤖 Воркеры → 💬 Автоответ): on/off, the text and reply stats.

The loop itself is bot/services/autoreply.py; it reads the live Settings each round, so
what is changed here takes effect on its next round.
"""

import html
import os
from datetime import datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.callbacks import ChoiceCB, MenuAction, MenuCB
from bot.keyboards.menu import main_menu
from bot.routers._common import require_text
from bot.routers.accounts import _send_chunked
from bot.services import autoreply
from bot.services.delegation import WorkerPool
from bot.states import SetAutoreplyText
from modules.settings import Settings

router = Router()

TOP_WORKERS = 10  # per-worker lines on the screen: a message holds 4096 characters


def _worker_lines(stats: dict, pool: WorkerPool) -> list[str]:
    """• @worker — N for every worker with replies, most replies first."""
    lines = []
    for path, count in stats["per_worker"].most_common():
        label = pool.storage.usernames.get(path)
        label = f"@{label}" if label else os.path.basename(path)
        lines.append(f"• {html.escape(label)} — {count}")
    return lines


def _view(settings: Settings, pool: WorkerPool) -> tuple[str, InlineKeyboardMarkup]:
    stats = autoreply.reply_stats(autoreply.load_replied(), datetime.now())
    return _screen(settings, stats, pool), _kb(settings, len(stats["per_worker"]) > TOP_WORKERS)


def _screen(settings: Settings, stats: dict, pool: WorkerPool) -> str:
    on = settings.autoreply_enabled and settings.autoreply_text
    status = "✅ включён" if on else "⏸ выключен"
    if settings.autoreply_enabled and not settings.autoreply_text:
        status += " (нет текста)"
    text = settings.autoreply_text and f"<blockquote>{html.escape(settings.autoreply_text)}</blockquote>"

    lines = [
        f"💬 <b>Автоответ</b>: {status}",
        f"Проверка воркеров: раз в {max(1, settings.autoreply_interval // 60)} мин.",
        "",
        "Тем, кто ответил на рассылку в ЛС, воркер один раз отвечает этим текстом, "
        "а их сообщения пересылаются сюда.",
        "",
        "<b>Текст:</b>",
        text or "— не задан",
        "",
        f"📊 <b>Ответили на рассылку</b>: всего {stats['total']} · за сутки {stats['day']} · "
        f"за 7 дней {stats['week']}",
    ]
    lines += _worker_lines(stats, pool)[:TOP_WORKERS]
    rest = stats["per_worker"].most_common()[TOP_WORKERS:]  # the 📋 button lists them
    if rest:
        lines.append(f"…и ещё воркеров: {len(rest)}, ответов у них: {sum(count for _, count in rest)}")
    return "\n".join(lines)


def _kb(settings: Settings, all_workers: bool) -> InlineKeyboardMarkup:
    def button(text, callback_data):
        return InlineKeyboardButton(text=text, callback_data=callback_data.pack())

    toggle = "⏸ Выключить" if settings.autoreply_enabled else "▶️ Включить"
    return InlineKeyboardMarkup(inline_keyboard=[
        [button(toggle, ChoiceCB(scope="autoreply", value="toggle"))],
        [button("✏️ Изменить текст", ChoiceCB(scope="autoreply", value="text"))],
        *([[button("📋 Все воркеры", ChoiceCB(scope="autoreply", value="workers"))]] if all_workers else []),
        [button("⬅️ Назад", MenuCB(action=MenuAction.WORKERS))],
    ])


@router.callback_query(MenuCB.filter(F.action == MenuAction.AUTOREPLY))
async def autoreply_screen(callback: CallbackQuery, state: FSMContext, settings: Settings, pool: WorkerPool):
    await state.clear()
    # navigation edits the workers screen in place, as the worker groups do
    text, markup = _view(settings, pool)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    await callback.answer()


@router.callback_query(ChoiceCB.filter((F.scope == "autoreply") & (F.value == "toggle")))
async def autoreply_toggle(callback: CallbackQuery, settings: Settings, pool: WorkerPool):
    enabled = not settings.autoreply_enabled
    if enabled and not settings.autoreply_text:
        await callback.answer("Сначала задайте текст автоответа.", show_alert=True)
        return

    try:
        settings.set_autoreply_enabled(enabled)
    except (OSError, ValueError) as err:  # ValueError: a config.toml broken by hand meanwhile
        await callback.answer(f"Не удалось сохранить config.toml: {err}", show_alert=True)
        return
    text, markup = _view(settings, pool)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    await callback.answer("Включён" if enabled else "Выключен")


@router.callback_query(ChoiceCB.filter((F.scope == "autoreply") & (F.value == "workers")))
async def autoreply_workers(callback: CallbackQuery, pool: WorkerPool):
    """The full per-worker list (the screen shows the top TOP_WORKERS), in message-sized chunks."""
    await callback.answer()
    stats = autoreply.reply_stats(autoreply.load_replied(), datetime.now())
    await _send_chunked(callback.message, "📊 <b>Ответили на рассылку — по воркерам</b>",
                        _worker_lines(stats, pool), sep="\n")


@router.callback_query(ChoiceCB.filter((F.scope == "autoreply") & (F.value == "text")))
async def autoreply_text_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(SetAutoreplyText.input)
    await callback.message.answer(
        "Пришлите новый текст автоответа одним сообщением, например:\n"
        "<code>Спасибо за ответ! Для дальнейшей связи напишите @MainMask</code>\n\n"
        "/cancel — отмена.",
        parse_mode="HTML",
    )


@router.message(SetAutoreplyText.input)
async def autoreply_text_apply(message: Message, state: FSMContext, settings: Settings):
    text = await require_text(message)
    if text is None:
        return

    try:
        settings.set_autoreply_text(text)
    except (OSError, ValueError) as err:  # ValueError: a config.toml broken by hand meanwhile
        await message.answer(f"⚠️ Не удалось сохранить config.toml: {err}")
        return
    await state.clear()
    note = "" if settings.autoreply_enabled else "\nАвтоответ выключен — включите его в 🤖 Воркеры → 💬 Автоответ."
    await message.answer(f"✅ Текст автоответа сохранён, действует со следующей проверки.{note}",
                         reply_markup=main_menu())
