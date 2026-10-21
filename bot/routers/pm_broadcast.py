import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import FunctionCB
from bot.keyboards.common import cancel_kb
from bot.routers._common import SEND_MESSAGE_PROMPT, build_content, busy_step, require_text, resolve, start_form
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from bot.states import PmBroadcast
from modules import login as sign_in
from modules.registry import ACTIVITY

router = Router()


@router.callback_query(FunctionCB.filter(F.key == "pm"))
async def start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool):
    await start_form(callback, state, PmBroadcast.peer, "✉️ ЛС человеку — кому пишут все воркеры? Пришлите "
                     "@username, ссылку t.me или номер телефона:", pool)


def is_phone(peer: str) -> bool:
    """A phone number (+7 999 123-45-67), not a @username or a t.me link: a username never starts with a digit."""
    return re.fullmatch(r"\+?[\d\s()-]+", peer) is not None and sign_in.normalize_phone(peer) is not None


@router.message(PmBroadcast.peer)
async def got_peer(message: Message, state: FSMContext):
    peer = await require_text(message)
    if peer is None:
        return
    await state.update_data(peer=peer, by_phone=is_phone(peer))
    await state.set_state(PmBroadcast.message)
    await message.answer(SEND_MESSAGE_PROMPT, reply_markup=cancel_kb(PmBroadcast.message))


@router.message(PmBroadcast.message)
async def got_message(
    message: Message,
    state: FSMContext,
    album,
    pool: WorkerPool,
    functions: dict,
    manager: JobManager,
):
    content = await build_content(message, album)
    if content is None:  # the step stays: another message is asked for
        return
    if manager.active:  # no await from here to manager.run: the slot it saw free is the one taken
        content.cleanup()
        await busy_step(message, manager, ACTIVITY)
        return
    data = await state.get_data()
    await state.clear()

    instance, bot_function = resolve(functions, "pm")

    await manager.run(
        message.bot, message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(data["peer"], content, data.get("by_phone", False), r),
        "Рассылка в ЛС…", cleanup=content.cleanup,
    )
