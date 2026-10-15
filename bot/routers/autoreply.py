"""The auto-reply screen (⚙️ Настройки → 📬 Автоответ): on/off, the text and reply stats.

The loop itself is bot/services/autoreply.py; it reads the live Settings each round, so
what is changed here takes effect on its next round.
"""

import html
import os
from datetime import datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.callbacks import ChoiceCB, MenuAction, MenuCB, NavCB
from bot.keyboards.common import cancel_kb
from bot.routers._common import leave_form, require_text, send_chunked
from bot.services import autoreply
from bot.services.workspaces import Workspaces
from modules.registry import SETTINGS
from bot.states import SetAutoreplyText
from modules.settings import Settings

router = Router()

TOP_WORKERS = 10  # per-worker lines on the screen: a message holds 4096 characters
MAX_SHOWN_TEXT = 500  # the auto-reply text's part on the screen (escaped it may grow fivefold)


def _worker_lines(stats: dict, workspaces: Workspaces) -> list[str]:
    """• @worker — N for every worker with replies, most replies first."""
    lines = []
    for path, count in stats["per_worker"].most_common():
        label = workspaces.username(path)
        label = f"@{label}" if label else os.path.basename(path)
        lines.append(f"• {html.escape(label)} — {count}")
    return lines


def _view(settings: Settings, workspaces: Workspaces) -> tuple[str, InlineKeyboardMarkup]:
    stats = autoreply.reply_stats(autoreply.load_replied(), datetime.now())
    return _screen(settings, stats, workspaces), _kb(settings, len(stats["per_worker"]) > TOP_WORKERS)


def _screen(settings: Settings, stats: dict, workspaces: Workspaces) -> str:
    on = settings.autoreply_enabled and settings.autoreply_text
    status = "✅ включён" if on else "⏸ выключен"
    if settings.autoreply_enabled and not settings.autoreply_text:
        status += " (нет текста)"
    shown = settings.autoreply_text[:MAX_SHOWN_TEXT] + ("…" if len(settings.autoreply_text) > MAX_SHOWN_TEXT else "")
    text = settings.autoreply_text and f"<blockquote>{html.escape(shown)}</blockquote>"

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
    lines += _worker_lines(stats, workspaces)[:TOP_WORKERS]
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
        [button("⬅️ Назад", NavCB(screen=SETTINGS))],
    ])


@router.callback_query(MenuCB.filter(F.action == MenuAction.AUTOREPLY))
async def autoreply_screen(callback: CallbackQuery, state: FSMContext, settings: Settings, workspaces: Workspaces):
    await leave_form(callback.message, state)
    # navigation edits the settings screen in place, as the worker groups do
    text, markup = _view(settings, workspaces)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    await callback.answer()


@router.callback_query(ChoiceCB.filter((F.scope == "autoreply") & (F.value == "toggle")))
async def autoreply_toggle(callback: CallbackQuery, settings: Settings, workspaces: Workspaces):
    enabled = not settings.autoreply_enabled
    if enabled and not settings.autoreply_text:
        await callback.answer("Сначала задайте текст автоответа.", show_alert=True)
        return

    try:
        settings.set_autoreply_enabled(enabled)
    except (OSError, ValueError) as err:  # ValueError: a config.toml broken by hand meanwhile
        await callback.answer(f"Не удалось сохранить config.toml: {err}"[:200], show_alert=True)  # Telegram's cap
        return
    text, markup = _view(settings, workspaces)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    await callback.answer("Включён" if enabled else "Выключен")


@router.callback_query(ChoiceCB.filter((F.scope == "autoreply") & (F.value == "workers")))
async def autoreply_workers(callback: CallbackQuery, workspaces: Workspaces):
    """The full per-worker list (the screen shows the top TOP_WORKERS), in message-sized chunks."""
    await callback.answer()
    stats = autoreply.reply_stats(autoreply.load_replied(), datetime.now())
    await send_chunked(callback.message, "📊 <b>Ответили на рассылку — по воркерам</b>",
                        _worker_lines(stats, workspaces), sep="\n")


@router.callback_query(ChoiceCB.filter((F.scope == "autoreply") & (F.value == "text")))
async def autoreply_text_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(SetAutoreplyText.input)
    await callback.message.answer(
        "Пришлите новый текст автоответа одним сообщением, например:\n"
        "<code>Спасибо за ответ! Для дальнейшей связи напишите @MainMask</code>",
        parse_mode="HTML", reply_markup=cancel_kb(SetAutoreplyText.input),
    )


@router.message(SetAutoreplyText.input)
async def autoreply_text_apply(message: Message, state: FSMContext, settings: Settings, workspaces: Workspaces):
    text = await require_text(message)
    if text is None:
        return

    try:
        settings.set_autoreply_text(text)
    except (OSError, ValueError) as err:  # ValueError: a config.toml broken by hand meanwhile
        await message.answer(f"⚠️ Не удалось сохранить config.toml: {err}")
        return
    await state.clear()
    # the screen again, with the new text and its ▶️ Включить if it is off
    text, markup = _view(settings, workspaces)
    await message.answer(f"✅ Текст автоответа сохранён, действует со следующей проверки.\n\n{text}",
                         parse_mode="HTML", reply_markup=markup)
