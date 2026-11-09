"""The auto-reply screen (🎯 База и рассылка → 📬 Автоответ, its view): the project's on/off, its text and its
workers' reply stats.

The loop itself is bot/services/autoreply.py; it reads each project's settings every round, so
what is changed here takes effect on its next round.
"""

import html
import os
from datetime import datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from bot.callbacks import AutoreplyCB
from bot.keyboards.common import button, cancel_kb
from bot.keyboards.menu import back_kb, screen_kb
from bot.routers._common import require_text, send_chunked
from bot.services import autoreply, screens
from bot.services.delegation import WorkerPool
from bot.services.workspaces import Workspace, Workspaces
from modules.registry import AUTOREPLY
from bot.states import SetAutoreplyText
from modules.settings import Settings

router = Router()

TOP_WORKERS = 10  # per-worker lines on the screen: a message holds 4096 characters
MAX_SHOWN_TEXT = 500  # the auto-reply text's part on the screen (escaped it may grow fivefold)


def _stats(pool: WorkerPool) -> dict:
    """The project's workers' replies (autoreply.reply_stats)."""
    return autoreply.reply_stats(autoreply.load_replied(), datetime.now(), pool.storage.directory)


def _worker_lines(stats: dict, workspaces: Workspaces) -> list[str]:
    """• @worker — N for every worker with replies, most replies first."""
    lines = []
    for path, count in stats["per_worker"].most_common():
        label = workspaces.username(path)
        label = f"@{label}" if label else os.path.basename(path)
        lines.append(f"• {html.escape(label)} — {count}")
    return lines


def _view(settings: Settings, pool: WorkerPool, workspaces: Workspaces,
          project: int) -> tuple[str, InlineKeyboardMarkup]:
    own, stats = autoreply.project_settings(pool.storage.home, settings), _stats(pool)
    return (_screen(own, settings, stats, workspaces, project),
            _kb(own, len(stats["per_worker"]) > TOP_WORKERS, project))


def _screen(own: dict, settings: Settings, stats: dict, workspaces: Workspaces, project: int) -> str:
    status = "✅ включён" if own["enabled"] and own["text"] else "⭕ выключен"
    if own["enabled"] and not own["text"]:
        status += " (нет текста)"
    shown = own["text"][:MAX_SHOWN_TEXT] + ("…" if len(own["text"]) > MAX_SHOWN_TEXT else "")
    text = own["text"] and f"<blockquote>{html.escape(shown)}</blockquote>"

    lines = [
        f"{screens.title_line(AUTOREPLY, screens.project_name(project))} — {status}",
        f"Проверка воркеров: раз в {max(1, settings.autoreply_interval // 60)} мин.",
        "",
        "Тем, кто ответил на рассылку в ЛС, воркер проекта один раз отвечает этим текстом, "
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


def _kb(own: dict, all_workers: bool, project: int) -> InlineKeyboardMarkup:
    """Its own buttons (screen_kb's `own`): on/off and the text, the full per-worker list when the screen shows only
    the top; its 🔄 (the stats grow as people answer): the registry's (Screen.refresh)."""
    toggle = "⏸ Выключить" if own["enabled"] else "▶️ Включить"
    return screen_kb(AUTOREPLY, project, own=[
        [button(toggle, AutoreplyCB(action="toggle", project=project)),
         button("✏️ Изменить текст", AutoreplyCB(action="text", project=project))],
        *([[button("📋 Все воркеры", AutoreplyCB(action="workers", project=project))]] if all_workers else []),
    ])


@screens.view(AUTOREPLY)
async def autoreply_screen(message: Message, state: FSMContext, edit: bool, settings: Settings, pool: WorkerPool,
                           workspace: Workspace, workspaces: Workspaces, **_):
    text, markup = _view(settings, pool, workspaces, workspace.project_id)
    await screens.send(message, edit)(text, parse_mode="HTML", reply_markup=markup)


@router.callback_query(AutoreplyCB.filter(F.action == "toggle"))
async def autoreply_toggle(callback: CallbackQuery, settings: Settings, pool: WorkerPool, workspace: Workspace,
                           workspaces: Workspaces):
    home = pool.storage.home
    own = autoreply.project_settings(home, settings)
    enabled = not own["enabled"]
    if enabled and not own["text"]:
        await callback.answer("Сначала задайте текст автоответа.", show_alert=True)
        return

    try:
        autoreply.save_project_settings(home, enabled, own["text"])
    except OSError as err:
        await callback.answer(f"⚠️ Не удалось сохранить: {err}"[:200], show_alert=True)  # Telegram's cap
        return
    text, markup = _view(settings, pool, workspaces, workspace.project_id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    await callback.answer("Включён" if enabled else "Выключен")


@router.callback_query(AutoreplyCB.filter(F.action == "workers"))
async def autoreply_workers(callback: CallbackQuery, pool: WorkerPool, workspace: Workspace,
                            workspaces: Workspaces):
    """The full per-worker list (the screen shows the top TOP_WORKERS), in message-sized chunks."""
    await callback.answer()
    await send_chunked(callback.message, "📊 <b>Ответили на рассылку — по воркерам</b>",
                       _worker_lines(_stats(pool), workspaces), sep="\n",
                       reply_markup=back_kb(AUTOREPLY, workspace.project_id, fresh=False))


@router.callback_query(AutoreplyCB.filter(F.action == "text"))
async def autoreply_text_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()  # an abandoned flow's data must not reach this one
    await state.set_state(SetAutoreplyText.input)
    await callback.message.answer(
        f"{AUTOREPLY} — пришлите новый текст одним сообщением, например:\n"
        "<code>Спасибо за ответ! Для дальнейшей связи напишите @your_manager</code>",
        parse_mode="HTML", reply_markup=cancel_kb(SetAutoreplyText.input),
    )


@router.message(SetAutoreplyText.input)
async def autoreply_text_apply(message: Message, state: FSMContext, settings: Settings, pool: WorkerPool,
                               workspace: Workspace, workspaces: Workspaces):
    text = await require_text(message)
    if text is None:
        return

    home = pool.storage.home
    try:
        autoreply.save_project_settings(home, autoreply.project_settings(home, settings)["enabled"], text)
    except OSError as err:
        await message.answer(f"⚠️ Не удалось сохранить: {err}")
        return
    await state.clear()
    # the screen again, with the new text and its ▶️ Включить if it is off
    text, markup = _view(settings, pool, workspaces, workspace.project_id)
    await message.answer(f"✅ Текст автоответа сохранён, действует со следующей проверки.\n\n{text}",
                         parse_mode="HTML", reply_markup=markup)
