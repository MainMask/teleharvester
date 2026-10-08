import datetime

from aiogram import F, Router
from aiogram.types import CallbackQuery, InaccessibleMessage

from bot.callbacks import ChoiceCB, FunctionCB
from bot.keyboards.common import yes_no_kb
from bot.routers._common import ensure_workers, resolve
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager

router = Router()

# the confirmation of an irreversible wipe: an old message's «Да» must not run it days later
CONFIRM_TTL = datetime.timedelta(minutes=10)


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
    message = callback.message  # inaccessible: too old for the bot to read, its date is 0
    if callback_data.value == "yes" and (isinstance(message, InaccessibleMessage) or
                                         datetime.datetime.now(datetime.timezone.utc) - message.date > CONFIRM_TTL):
        await callback.answer("Подтверждение устарело — запустите «Очистить диалоги» заново.", show_alert=True)
        return
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
