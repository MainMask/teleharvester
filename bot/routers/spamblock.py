import datetime

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from bot.callbacks import ChoiceCB, FunctionCB, ReleaseCB
from bot.keyboards.common import choice_kb
from bot.keyboards.menu import back_kb
from bot.routers._common import busy_tap, confirm_expired, ensure_workers, resolve
from bot.routers.accounts.proxies import proxies_busy, rehome_now
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from bot.services.workspaces import Workspace, Workspaces
from functions.spamblock import rehome, worker_name
from modules import restricted_workers
from modules.registry import ACCOUNT_LIST, menu_path, title

router = Router()

# a decision rests on the @SpamBot reply shown with the button: an older one may be out of date
# (the worker clean again since, then restricted anew), so the admin checks again
BUTTON_TTL = datetime.timedelta(hours=24)


async def offer_release(bot, chat_id, func):
    """After a status check: a message with the button for each permanently restricted
    worker whose contacts wait for the admin's decision."""
    for _, user_id, name, people in func.waiting():
        await bot.send_message(
            chat_id,
            f"⛔ {name} — ограничен бессрочно\n"
            f"Его контакты ({people}) ждут решения: пока им никто не пишет.\n"
            "Если ограничение точно бессрочное (ответ @SpamBot выше), передайте их другим "
            "воркерам. Это не отменить. Кнопка действует сутки.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text=f"📤 Передать контакты ({people})", callback_data=ReleaseCB(user_id=user_id).pack(),
            )]]),
        )


async def offer_rehome(bot, chat_id, workspace):
    """After a status check: the button that moves the project's permanently restricted workers onto its
    proxies for them (and the ones clean again back), when there are any to move. Not mid-check: a proxy
    change rebuilds the clients a job holds. The button names its project: pressed from another one later,
    it still moves this project's workers."""
    try:
        pending = rehome(workspace.pool.storage, dry_run=True)
    except ValueError as err:  # a list edited by hand
        await bot.send_message(chat_id, f"⚠️ Пересадка ограниченных невозможна: {err}. Исправьте: {menu_path('proxy')}.",
                               reply_markup=back_kb(ACCOUNT_LIST, workspace.project_id))
        return
    if not (pending["out"] or pending["back"]):
        return
    await bot.send_message(
        chat_id,
        f"🔁 Бессрочно ограниченных — на их прокси: {len(pending['out'])}; остальных вне основного пула "
        f"(сняты с ограничения или перенесены из другого проекта) — в пул: {len(pending['back'])}. "
        "Пересадить? Кнопка действует сутки.",
        reply_markup=choice_kb("rehome", [("🔁 Пересадить", str(workspace.project_id))]),
    )


@router.callback_query(FunctionCB.filter(F.key == "status"))
async def run_status(callback: CallbackQuery, pool: WorkerPool, functions: dict, manager: JobManager,
                     workspace: Workspace):
    if await busy_tap(callback, manager) or not await ensure_workers(callback, pool):
        return

    async def status(func, report):
        await func.run(report, replies=True)
        await offer_release(callback.bot, callback.message.chat.id, func)
        await offer_rehome(callback.bot, callback.message.chat.id, workspace)

    instance, bot_function = resolve(functions, "status")
    await manager.run(
        callback.bot, callback.message.chat.id, pool, instance, bot_function, status,
        "Проверка SpamBot…",
    )


STALE_CHECK = f"Кнопка устарела: запустите «{title('status')}» заново — бот пришлёт новую."


# the worker's project, whichever the chat is in now (or none, after a restart): the button is 24 h old at most
@router.callback_query(ReleaseCB.filter(), flags={"any_project": True})
async def release(callback: CallbackQuery, callback_data: ReleaseCB, workspaces: Workspaces):
    if confirm_expired(callback.message, BUTTON_TTL):
        await callback.answer(STALE_CHECK, show_alert=True)
        return

    storage, path, project = next(((workspace.pool.storage, path, workspace.project_id)
                                   for workspace in workspaces.all()
                                   for path, js in workspace.pool.storage.jsessions_paths.items()
                                   if js.account.account.user_id == callback_data.user_id), (None, None, 0))
    back = back_kb(ACCOUNT_LIST, project, fresh=False)
    # checked again since the message: clean now, or the session is gone
    if path is None or path not in restricted_workers.load():
        await callback.message.edit_text("Воркер уже не ограничен бессрочно — передавать нечего.", reply_markup=back)
    else:
        restricted_workers.release(path)
        await callback.message.edit_text(
            f"✅ Контакты {worker_name(storage, path)} переданы: со следующего запуска им "
            f"напишут другие воркеры, а «{title('addcontacts')}» добавит их заново.", reply_markup=back
        )
    await callback.answer()


# the project the button names, whichever the chat is in now (or none, after a restart)
@router.callback_query(ChoiceCB.filter(F.scope == "rehome"), flags={"any_project": True})
async def rehome_restricted(callback: CallbackQuery, callback_data: ChoiceCB, workspaces: Workspaces):
    message = callback.message
    # a project id; "go" on a button sent before it carried one
    workspace = workspaces.get(int(callback_data.value)) if callback_data.value.isdigit() else None
    if workspace is None or confirm_expired(message, BUTTON_TTL):
        await callback.answer(STALE_CHECK, show_alert=True)
        return
    await callback.answer()
    pool = workspace.pool
    if await proxies_busy(message, pool, workspace.manager):
        return
    if report := await rehome_now(message, pool):
        await message.edit_text(report, reply_markup=back_kb(ACCOUNT_LIST, workspace.project_id, fresh=False))
