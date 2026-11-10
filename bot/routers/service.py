from aiogram import F, Router
from aiogram.types import CallbackQuery

from bot.callbacks import ChoiceCB, FunctionCB
from bot.keyboards.common import choice_kb
from bot.routers._common import busy_tap, confirm_expired, ensure_workers, resolve, stale_tap
from bot.services import screens
from bot.services.screens import head
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from bot.services.workspaces import Workspace
from modules.registry import SECURITY, title

router = Router()

# --- clear dialogs (destructive → confirm) ---

@router.callback_query(FunctionCB.filter(F.key == "clear"))
async def clear_start(callback: CallbackQuery, pool: WorkerPool, workspace: Workspace):
    if not await ensure_workers(callback, pool):
        return
    project = workspace.project_id  # the question names its project: see clear_run
    await callback.message.edit_text(  # in 🔐's place: ✖️ brings it back
        f"{head(title('clear'))} — ⚠️ очистить ВСЕ диалоги на воркерах (и выйти из каналов)? Это необратимо.",
        parse_mode="HTML",
        reply_markup=choice_kb("clear_confirm", [("🧹 Да, очистить", f"yes.{project}"),
                                                 ("✖️ Отмена", f"no.{project}")], widths=(2,)),
    )


@router.callback_query(ChoiceCB.filter(F.scope == "clear_confirm"))
async def clear_run(callback: CallbackQuery, callback_data: ChoiceCB, pool: WorkerPool, functions: dict,
                    manager: JobManager, workspace: Workspace):
    answer, _, project = callback_data.value.partition(".")
    # asked in another project (the chat moved since): it must not wipe this one's workers
    if project != str(workspace.project_id):
        await stale_tap(callback)
        return
    if answer == "yes" and confirm_expired(callback.message):
        await callback.answer(f"Подтверждение устарело — запустите «{title('clear')}» заново.", show_alert=True)
        return
    if answer == "yes" and await busy_tap(callback, manager):
        return
    await callback.answer()

    if answer != "yes":  # 🔐 Безопасность back in the question's place
        await screens.show(callback.message, SECURITY, True, project=workspace.project_id)
        return
    await callback.message.edit_reply_markup(reply_markup=None)  # asked once: no second wipe from it

    instance, bot_function = resolve(functions, "clear")
    await manager.run(
        callback.bot, callback.message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(r),
        "Очистка диалогов…",
    )
