from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import FunctionCB
from bot.routers._common import ensure_workers, require_text, resolve
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from bot.states import Invite
from modules.settings import Settings

router = Router()


@router.callback_query(FunctionCB.filter(F.key == "invite"))
async def start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool):
    await callback.answer()

    if not await ensure_workers(callback, pool):
        return

    await state.clear()
    await state.set_state(Invite.source)
    await callback.message.answer("Инвайтинг. Ссылка на чат-источник (откуда брать участников):")


@router.message(Invite.source)
async def got_source(message: Message, state: FSMContext):
    source = await require_text(message)
    if source is None:
        return
    await state.update_data(source=source)
    await state.set_state(Invite.destination)
    await message.answer("Куда приглашать (ссылка/username чата назначения):")


@router.message(Invite.destination)
async def got_destination(
    message: Message,
    state: FSMContext,
    pool: WorkerPool,
    functions: dict,
    manager: JobManager,
    settings: Settings,
):
    destination = await require_text(message)
    if destination is None:
        return

    data = await state.get_data()
    await state.clear()

    instance, bot_function = resolve(functions, "invite")
    await manager.run(
        message.bot, message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(data["source"], destination, settings.delay, r),
        "Инвайтинг…", "Инвайтинг завершён ✅",
    )
