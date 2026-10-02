from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import ChoiceCB, FunctionCB
from bot.keyboards.common import choice_kb
from bot.routers._common import SEND_MESSAGE_PROMPT, build_content, ensure_workers, require_text, resolve
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from bot.states import PmBroadcast
from modules.settings import Settings

router = Router()


@router.callback_query(FunctionCB.filter(F.key == "pm"))
async def start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool):
    await callback.answer()

    if not await ensure_workers(callback, pool):
        return

    await state.clear()
    await callback.message.answer(
        "Рассылка в ЛС. Кому отправляем?",
        reply_markup=choice_kb("pm_mode", [("По username", "username"), ("По номеру", "phone")]),
    )


@router.callback_query(ChoiceCB.filter(F.scope == "pm_mode"))
async def pick_mode(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext):
    await state.update_data(by_phone=callback_data.value == "phone")
    await state.set_state(PmBroadcast.peer)

    label = "номер телефона" if callback_data.value == "phone" else "username"
    await callback.message.answer(f"Введите {label} получателя:")
    await callback.answer()


@router.message(PmBroadcast.peer)
async def got_peer(message: Message, state: FSMContext):
    peer = await require_text(message)
    if peer is None:
        return
    await state.update_data(peer=peer)
    await state.set_state(PmBroadcast.message)
    await message.answer(SEND_MESSAGE_PROMPT)


@router.message(PmBroadcast.message)
async def got_message(
    message: Message,
    state: FSMContext,
    album,
    pool: WorkerPool,
    functions: dict,
    manager: JobManager,
    settings: Settings,
):
    data = await state.get_data()
    await state.clear()

    content = await build_content(message, album)
    if content is None:
        return

    instance, bot_function = resolve(functions, "pm")

    async def job(func, reporter):
        try:
            await func.run(
                data["peer"], content, data.get("by_phone", False), settings.delay, reporter,
            )
        finally:
            content.cleanup()

    started = await manager.run(
        message.bot, message.chat.id, pool, instance, bot_function, job,
        "Рассылка в ЛС…", "Рассылка завершена ✅",
    )
    if not started:
        content.cleanup()
