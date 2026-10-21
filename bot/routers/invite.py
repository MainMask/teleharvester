from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import FunctionCB
from bot.keyboards.common import cancel_kb
from bot.routers._common import require_text, resolve, start_form
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from bot.states import Invite

router = Router()


@router.callback_query(FunctionCB.filter(F.key == "invite"))
async def start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool):
    await start_form(callback, state, Invite.source,
                     "🤝 Инвайтинг — пришлите ссылку на чат-источник (откуда брать участников):", pool)


@router.message(Invite.source)
async def got_source(message: Message, state: FSMContext):
    source = await require_text(message)
    if source is None:
        return
    await state.update_data(source=source)
    await state.set_state(Invite.destination)
    await message.answer("Пришлите ссылку или @username чата, куда приглашать:",
                         reply_markup=cancel_kb(Invite.destination))


@router.message(Invite.destination)
async def got_destination(
    message: Message,
    state: FSMContext,
    pool: WorkerPool,
    functions: dict,
    manager: JobManager,
):
    destination = await require_text(message)
    if destination is None:
        return

    data = await state.get_data()
    await state.clear()

    instance, bot_function = resolve(functions, "invite")
    await manager.run(
        message.bot, message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(data["source"], destination, None, r),
        "Инвайтинг…", "✅ Инвайтинг завершён",
    )
