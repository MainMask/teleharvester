from aiogram import F, Router
from aiogram.types import CallbackQuery

from bot.callbacks import FunctionCB
from bot.routers._common import ensure_workers, resolve
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager

router = Router()


@router.callback_query(FunctionCB.filter(F.key == "status"))
async def run_status(callback: CallbackQuery, pool: WorkerPool, functions: dict, manager: JobManager):
    await callback.answer()
    if not await ensure_workers(callback, pool):
        return

    instance, bot_function = resolve(functions, "status")
    await manager.run(
        callback.bot, callback.message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(r, move_restricted=False),
        "Проверка статуса…", "Проверка статуса завершена ✅",
    )
