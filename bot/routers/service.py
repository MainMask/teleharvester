import datetime

from aiogram import F, Router
from aiogram.types import CallbackQuery, InaccessibleMessage

from bot.callbacks import ChoiceCB, FunctionCB
from bot.keyboards.common import choice_kb
from bot.keyboards.menu import back_kb
from bot.routers._common import ensure_workers, resolve
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from bot.services.workspaces import Workspace
from modules.registry import SECURITY

router = Router()

# the confirmation of an irreversible wipe: an old message's «Да» must not run it days later
CONFIRM_TTL = datetime.timedelta(minutes=10)


# --- clear dialogs (destructive → confirm) ---

@router.callback_query(FunctionCB.filter(F.key == "clear"))
async def clear_start(callback: CallbackQuery, pool: WorkerPool):
    if not await ensure_workers(callback, pool):
        return
    await callback.message.answer(  # a new message: its age (CONFIRM_TTL) is its own, not the screen's
        "🧹 Очистить диалоги — ⚠️ очистить ВСЕ диалоги на воркерах (и выйти из каналов)? Это необратимо.",
        reply_markup=choice_kb("clear_confirm", [("🧹 Да, очистить", "yes"), ("↩️ Отмена", "no")], widths=(2,)),
    )


@router.callback_query(ChoiceCB.filter(F.scope == "clear_confirm"))
async def clear_run(callback: CallbackQuery, callback_data: ChoiceCB, pool: WorkerPool, functions: dict,
                    manager: JobManager, workspace: Workspace):
    message = callback.message  # inaccessible: too old for the bot to read, its date is 0
    if callback_data.value == "yes" and (isinstance(message, InaccessibleMessage) or
                                         datetime.datetime.now(datetime.timezone.utc) - message.date > CONFIRM_TTL):
        await callback.answer("Подтверждение устарело — запустите «Очистить диалоги» заново.", show_alert=True)
        return
    await callback.answer()

    if callback_data.value != "yes":  # in the question's place, as /cancel
        await callback.message.edit_text("Отменено.", reply_markup=back_kb(SECURITY, workspace.project_id, fresh=False))
        return
    await callback.message.edit_reply_markup(reply_markup=None)  # asked once: no second wipe from it

    instance, bot_function = resolve(functions, "clear")
    await manager.run(
        callback.bot, callback.message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(r),
        "Очистка диалогов…",
    )
