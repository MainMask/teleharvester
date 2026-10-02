from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import ChoiceCB, FunctionCB
from bot.keyboards.common import choice_kb
from bot.routers._common import ensure_workers, require_text, resolve
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from bot.states import Join
from modules.settings import Settings

router = Router()


@router.callback_query(FunctionCB.filter(F.key == "join"))
async def start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool):
    await callback.answer()

    if not await ensure_workers(callback, pool):
        return

    await state.clear()
    await callback.message.answer(
        "Вступление в чат. Режим:",
        reply_markup=choice_kb("join_mode", [
            ("Просто вступить", "1"),
            ("Вступить в привязанный чат", "2"),
        ]),
    )


@router.callback_query(ChoiceCB.filter(F.scope == "join_mode"))
async def pick_mode(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext):
    await state.update_data(mode=callback_data.value)
    await state.set_state(Join.link)
    await callback.message.answer("Ссылка на чат/канал:")
    await callback.answer()


@router.message(Join.link)
async def got_link(
    message: Message,
    state: FSMContext,
    pool: WorkerPool,
    functions: dict,
    manager: JobManager,
    settings: Settings,
):
    link = await require_text(message)
    if link is None:
        return

    data = await state.get_data()
    await state.clear()

    if "mode" not in data:  # stale flow after a state clear
        await message.answer("Флоу устарел, начните заново.")
        return

    instance, bot_function = resolve(functions, "join")
    await manager.run(
        message.bot, message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(data["mode"], link, settings.delay, r),
        "Вступление…", "Вступление завершено ✅",
    )
