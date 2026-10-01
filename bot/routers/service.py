from aiogram import F, Router
from aiogram.types import CallbackQuery

from bot.callbacks import ChoiceCB, FunctionCB
from bot.keyboards.common import yes_no_kb
from bot.routers._common import ensure_workers, resolve
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager

router = Router()


# --- statistics (phone numbers) ---

@router.callback_query(FunctionCB.filter(F.key == "stats"))
async def stats_run(callback: CallbackQuery, pool: WorkerPool, functions: dict, manager: JobManager):
    await callback.answer()
    if not await ensure_workers(callback, pool):
        return
    instance, bot_function = resolve(functions, "stats")
    await manager.run(
        callback.bot, callback.message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(r),
        "Статистика по номерам…", "Готово ✅",
    )


# --- terminate other sessions ---

@router.callback_query(FunctionCB.filter(F.key == "terminate"))
async def terminate_run(callback: CallbackQuery, pool: WorkerPool, functions: dict, manager: JobManager):
    await callback.answer()
    if not await ensure_workers(callback, pool):
        return
    instance, bot_function = resolve(functions, "terminate")
    await manager.run(
        callback.bot, callback.message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(r),
        "Сброс чужих сессий…", "Готово ✅",
    )


# --- clear dialogs (destructive → confirm) ---

@router.callback_query(FunctionCB.filter(F.key == "clear"))
async def clear_start(callback: CallbackQuery, pool: WorkerPool):
    await callback.answer()
    if not await ensure_workers(callback, pool):
        return
    await callback.message.answer(
        "⚠️ Очистить ВСЕ диалоги на воркерах (и выйти из каналов)? Это необратимо.",
        reply_markup=yes_no_kb("clear_confirm"),
    )


@router.callback_query(ChoiceCB.filter(F.scope == "clear_confirm"))
async def clear_run(callback: CallbackQuery, callback_data: ChoiceCB, pool: WorkerPool, functions: dict, manager: JobManager):
    await callback.answer()

    if callback_data.value != "yes":
        await callback.message.answer("Отменено.")
        return

    instance, bot_function = resolve(functions, "clear")
    await manager.run(
        callback.bot, callback.message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(r),
        "Очистка диалогов…", "Готово ✅",
    )
