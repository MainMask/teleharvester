import datetime

from aiogram import F, Router
from aiogram.types import CallbackQuery, InaccessibleMessage, InlineKeyboardButton, InlineKeyboardMarkup

from bot.callbacks import FunctionCB, ReleaseCB
from bot.routers._common import ensure_workers, resolve
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from bot.services.workspaces import Workspaces
from functions.spamblock import worker_name
from modules import restricted_workers

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


@router.callback_query(FunctionCB.filter(F.key == "status"))
async def run_status(callback: CallbackQuery, pool: WorkerPool, functions: dict, manager: JobManager):
    await callback.answer()
    if not await ensure_workers(callback, pool):
        return

    async def status(func, report):
        await func.run(report, replies=True)
        await offer_release(callback.bot, callback.message.chat.id, func)

    instance, bot_function = resolve(functions, "status")
    await manager.run(
        callback.bot, callback.message.chat.id, pool, instance, bot_function, status,
        "Проверка статуса…", "Проверка статуса завершена ✅",
    )


# the worker's project, whichever the chat is in now (or none, after a restart): the button is 24 h old at most
@router.callback_query(ReleaseCB.filter(), flags={"any_project": True})
async def release(callback: CallbackQuery, callback_data: ReleaseCB, workspaces: Workspaces):
    message = callback.message  # inaccessible: too old for the bot to read, its date is 0
    if isinstance(message, InaccessibleMessage) or \
            datetime.datetime.now(datetime.timezone.utc) - message.date > BUTTON_TTL:
        await callback.answer("Кнопка устарела: запустите «Проверку статуса» заново — бот пришлёт новую.",
                              show_alert=True)
        return

    storage, path = next(((workspace.pool.storage, path) for workspace in workspaces.all()
                          for path, js in workspace.pool.storage.jsessions_paths.items()
                          if js.account.account.user_id == callback_data.user_id), (None, None))
    # checked again since the message: clean now, or the session is gone
    if path is None or path not in restricted_workers.load():
        await callback.message.edit_text("Воркер уже не ограничен бессрочно — передавать нечего.")
    else:
        restricted_workers.release(path)
        await callback.message.edit_text(
            f"✅ Контакты {worker_name(storage, path)} переданы: со следующего запуска им "
            "напишут другие воркеры, а «📇 В контакты» добавит их заново."
        )
    await callback.answer()
