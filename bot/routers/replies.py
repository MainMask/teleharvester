"""✍️ Ответить under a forwarded mailing reply (bot/services/autoreply.py): the admin's answer, typed in the bot, sent
by the worker the person answered — on its own client (delegation.borrow), as the auto-reply is."""
import os

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import ReplyCB
from bot.keyboards.common import cancel_kb
from bot.routers._common import require_text
from bot.services.autoreply import DIALOGS_LIMIT, log_dialog
from bot.services.delegation import WorkerPool, borrow
from bot.states import ManualReply
from modules import rich_message

router = Router()

SEND_TIMEOUT = 60  # connect() and the send: a dead proxy must not hang the answer
WORKER_GONE = "Этого воркера больше нет в проекте — ответить от него нельзя."


def _worker(pool: WorkerPool, name: str):
    """(client, path, label) of the project's worker by its session file's name; None if it is gone."""
    path = os.path.join(pool.storage.directory, name)
    client = next((c for c in pool.workers if pool.storage.get_session_path(c) == path), None)
    if client is None:
        return None
    username = pool.storage.usernames.get(path)
    return client, path, f"@{username}" if username else name


async def _peer(client, user: int):
    """The person to write to: from the session's cache, else from the worker's recent chats (its cache is dropped
    between uses: release_client)."""
    try:
        return await client.get_input_entity(user)
    except ValueError:
        async for dialog in client.iter_dialogs(limit=DIALOGS_LIMIT):
            if dialog.entity.id == user:
                return dialog.entity
        raise ValueError("чат с этим человеком не найден среди последних диалогов воркера")


@router.callback_query(ReplyCB.filter())
async def reply_start(callback: CallbackQuery, callback_data: ReplyCB, state: FSMContext, pool: WorkerPool):
    found = _worker(pool, callback_data.worker)
    if found is None:
        await callback.answer(WORKER_GONE, show_alert=True)
        return
    await callback.answer()
    await state.clear()  # an abandoned flow's data must not reach this one
    await state.set_state(ManualReply.text)
    await state.update_data(reply_worker=callback_data.worker, reply_user=callback_data.user)
    # under the notification it answers: whose reply it is stays in sight
    await callback.message.reply(f"✍️ Ответ от {found[2]} — пришлите текст одним сообщением:",
                                 reply_markup=cancel_kb(ManualReply.text))


@router.message(ManualReply.text)
async def reply_send(message: Message, state: FSMContext, pool: WorkerPool):
    text = await require_text(message)
    if text is None:
        return
    data = await state.get_data()
    found = _worker(pool, data.get("reply_worker", ""))
    if found is None:
        await state.clear()
        await message.answer(WORKER_GONE)
        return
    client, path, label = found
    if pool.scraped(path):  # the scraper drives it from a client of its own: never two connections on one key
        await message.answer(f"⚠️ {label} сейчас занят скрапом — пришлите текст ещё раз, когда скрап закончится.")
        return

    async def send():
        await client.send_message(await _peer(client, data["reply_user"]), text, parse_mode=None)  # as typed

    try:
        await borrow(pool, client, path, send, SEND_TIMEOUT)
    except Exception as err:  # blocked, privacy, a flood limit, a dead proxy: the step stays for another try
        reason = rich_message.refusal_reason(err) or str(err) or type(err).__name__
        await message.answer(f"⚠️ Не отправлено от {label}: {reason}. Пришлите текст ещё раз или нажмите ✖️ Отмена.",
                             reply_markup=cancel_kb(ManualReply.text))
        return
    await state.clear()
    log_dialog(pool.storage.home, path, data["reply_user"], text, out=True)  # the report's dialog
    await message.answer(f"✅ Отправлено от {label}.")
