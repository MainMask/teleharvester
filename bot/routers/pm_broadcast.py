from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import ChoiceCB, FunctionCB
from bot.keyboards.common import choice_kb, yes_no_kb
from bot.routers._common import ensure_workers, resolve
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
    await state.update_data(peer=message.text.strip())
    await message.answer("Прикреплять медиа (случайный файл из media/)?", reply_markup=yes_no_kb("pm_media"))


@router.callback_query(ChoiceCB.filter(F.scope == "pm_media"))
async def pick_media(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext):
    await state.update_data(media=callback_data.value == "yes")
    await state.set_state(PmBroadcast.text)
    await callback.message.answer("Введите текст сообщения:")
    await callback.answer()


@router.message(PmBroadcast.text)
async def got_text(
    message: Message,
    state: FSMContext,
    pool: WorkerPool,
    functions: dict,
    manager: JobManager,
    settings: Settings,
):
    data = await state.get_data()
    await state.clear()

    instance, bot_function = resolve(functions, "pm")
    text = message.text
    await manager.run(
        message.bot, message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(
            data["peer"], text, data.get("media", False),
            data.get("by_phone", False), settings.delay, r,
        ),
        "Рассылка в ЛС…", "Рассылка завершена ✅",
    )
