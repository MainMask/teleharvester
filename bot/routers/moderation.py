from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import ChoiceCB, FunctionCB
from bot.keyboards.common import cancel_kb, choice_kb, stop_kb
from bot.keyboards.menu import back_kb
from modules.registry import ACTIVITY, BOT_FUNCTIONS_BY_KEY
from bot.routers._common import (STALE_BUTTON, STALE_STEP, back_answer, left_flow, require_text, resolve,
                                 stale_tap, start_form)
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager, busy
from bot.services.progress import Progress
from bot.services.runner import TelegramReporter
from bot.states import ReportMessage, ReportUser
from functions.report import ReportFunc
from modules.storages.sessions_storage import connect_client, release_client

router = Router()

REPORT = BOT_FUNCTIONS_BY_KEY["report"]
POSTS_ASK = "Номера постов через запятую — это число в конце ссылки на пост (t.me/name/123 → 123):"
COMMENT_ASK = "Комментарий для модераторов Telegram — пришлите текст или нажмите «⏭ Без комментария»:"

# chat_id -> (instance, flow-state dict, current options list) for the dynamic report flow
_FLOWS: dict[int, tuple] = {}


# =========================== report (user) ===========================

@router.callback_query(FunctionCB.filter(F.key == "reportuser"))
async def ru_start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool):
    await start_form(callback, state, ReportUser.username,
                     "🚫 Жалоба на пользователя — пришлите @username или ссылку на него:", pool)


@router.message(ReportUser.username)
async def ru_username(message: Message, state: FSMContext, functions: dict):
    username = await require_text(message)
    if username is None:
        return
    await state.update_data(username=username)

    instance, _ = resolve(functions, "reportuser")
    await message.answer("🚫 Жалоба на пользователя — причина:", reply_markup=cancel_kb(ReportUser.username, choice_kb(
        "reason", [(label, str(index)) for index, (label, _reason) in enumerate(instance.reasons)])))


@router.callback_query(ChoiceCB.filter(F.scope == "reason"))
async def ru_reason(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext):
    if await left_flow(callback, state, ReportUser.username):
        return
    await state.update_data(reason_index=int(callback_data.value))
    await state.set_state(ReportUser.comment)
    await callback.message.edit_text(COMMENT_ASK, reply_markup=_comment_kb(ReportUser.comment))
    await callback.answer()


def _comment_kb(step):
    return cancel_kb(step, choice_kb("rpt_nocomment", [("⏭ Без комментария", "go")]))


async def _typed_comment(message: Message) -> str | None:
    """The comment typed; None after asking again (no text). «-»: none, as before its button."""
    if not message.text:
        await message.answer("Нужен текст — или «⏭ Без комментария» выше. Пришлите ещё раз:")
        return None
    return "" if message.text.strip() == "-" else message.text


@router.message(ReportUser.comment)
async def ru_run(message: Message, state: FSMContext, pool: WorkerPool, functions: dict, manager: JobManager):
    comment = await _typed_comment(message)
    if comment is not None:
        await _report_user(message, state, pool, functions, manager, comment)


async def _report_user(message: Message, state: FSMContext, pool: WorkerPool, functions: dict, manager: JobManager,
                       comment: str):
    data = await state.get_data()
    await state.clear()

    if "username" not in data or "reason_index" not in data:  # stale flow after a state clear
        await back_answer(message, STALE_STEP, ACTIVITY, manager.project_id)
        return

    instance, bot_function = resolve(functions, "reportuser")
    reason_type = instance.reasons[data["reason_index"]][1]

    await manager.run(
        message.bot, message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(data["username"], reason_type, comment, r),
        "Жалоба на пользователя…",
    )


# ====================== report (message/post) ======================

def _free(manager: JobManager, pool: WorkerPool):
    """The report flow is over: its slot and its workers are free (a scrape may start on them)."""
    pool.in_job = []
    manager.release()


async def _abort(chat_id: int, pool: WorkerPool):
    """on_abort for the manager: disconnect the held worker and drop the flow."""
    pool.in_job = []  # the manager already freed the slot
    entry = _FLOWS.pop(chat_id, None)
    if entry is not None:
        await release_client(entry[1]["session"])


@router.callback_query(FunctionCB.filter(F.key == "report"))
async def rm_start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool):
    await start_form(callback, state, ReportMessage.link,
                     "🚩 Жалоба на пост — пришлите ссылку на канал или чат, или сразу на пост:", pool)


@router.message(ReportMessage.link)
async def rm_link(message: Message, state: FSMContext):
    link = await require_text(message)
    if link is None:
        return
    await state.update_data(link=link)
    await state.set_state(ReportMessage.ids)
    post = ReportFunc.post_id(link)
    if post is None:
        await message.answer(POSTS_ASK, reply_markup=cancel_kb(ReportMessage.ids))
        return
    await state.update_data(post=post)
    await message.answer(f"В ссылке пост {post}. Пожаловаться на него — или пришлите номера постов через запятую:",
                         reply_markup=cancel_kb(ReportMessage.ids,
                                                choice_kb("rm_post", [(f"✅ Только на пост {post}", "go")])))


@router.callback_query(ReportMessage.ids, ChoiceCB.filter(F.scope == "rm_post"))
async def rm_post(callback: CallbackQuery, state: FSMContext):
    post = (await state.get_data()).get("post")
    if post is None:  # a stale button
        await stale_tap(callback)
        return
    await callback.answer()
    await _ask_comment(callback.message, state, [post])


@router.message(ReportMessage.ids)
async def rm_ids(message: Message, state: FSMContext):
    parts = [p.strip() for p in (message.text or "").split(",") if p.strip()]
    if not parts or not all(p.isdecimal() for p in parts):
        await message.answer("Нужны номера постов через запятую, например 12, 15. Пришлите ещё раз:")
        return
    await _ask_comment(message, state, [int(p) for p in parts])


async def _ask_comment(message: Message, state: FSMContext, ids: list[int]):
    await state.update_data(ids=ids)
    await state.set_state(ReportMessage.comment)
    await message.answer(COMMENT_ASK, reply_markup=_comment_kb(ReportMessage.comment))


@router.message(ReportMessage.comment)
async def rm_begin(message: Message, state: FSMContext, pool: WorkerPool, functions: dict, manager: JobManager):
    comment = await _typed_comment(message)
    if comment is not None:
        await _report_posts(message, state, pool, functions, manager, comment)


@router.callback_query(ChoiceCB.filter(F.scope == "rpt_nocomment"))
async def no_comment(callback: CallbackQuery, state: FSMContext, pool: WorkerPool, functions: dict,
                     manager: JobManager):
    """⏭ Без комментария: the report (a user's or posts') goes on without one, only from its own step."""
    run = {ReportUser.comment.state: _report_user, ReportMessage.comment.state: _report_posts}.get(
        await state.get_state())
    if run is None:
        await stale_tap(callback)
        return
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)  # pressed once: one report
    await run(callback.message, state, pool, functions, manager, "")


async def _report_posts(message: Message, state: FSMContext, pool: WorkerPool, functions: dict, manager: JobManager,
                        comment: str):
    data = await state.get_data()
    await state.clear()

    chat_id = message.chat.id

    if not manager.acquire(REPORT.title, on_abort=lambda: _abort(chat_id, pool), timeout=600):
        await back_answer(message, busy(manager.label), ACTIVITY, manager.project_id)
        return

    instance, _ = resolve(functions, "report")
    workers = pool.delegate(instance)
    pool.in_job = workers  # the flow drives them until _free / _abort: no scrape starts on them

    if not workers:  # guard: never index into an empty pool with the slot held
        _free(manager, pool)
        await back_answer(message, "Нет воркеров.", ACTIVITY, manager.project_id)
        return

    first = workers[0]
    flow = {
        "session": first,
        "peer": data["link"],
        "ids": data["ids"],
        "comment": comment,
        "selections": [],
        "rest": workers[1:],
        "busy": False,  # the entry's empty options list already rejects taps until step()
    }
    # registered before the first await, so a /cancel or timeout meanwhile (_abort)
    # can disconnect the session; `entry` tells whether this flow still owns the slot
    entry = (instance, flow, [])
    _FLOWS[chat_id] = entry

    try:
        await connect_client(first)
        if _FLOWS.get(chat_id) is not entry:  # cancelled during connect: send no report
            await release_client(first)
            return
        status, options = await instance.step(flow, b"")
    except Exception as err:
        if _FLOWS.get(chat_id) is not entry:  # aborted meanwhile: the slot is no longer ours
            return
        _FLOWS.pop(chat_id, None)
        _free(manager, pool)  # before any await: a /cancel meanwhile must not free a foreign slot
        await release_client(first)
        await back_answer(message, f"⚠️ Ошибка: {err}", ACTIVITY, manager.project_id)
        return

    if _FLOWS.get(chat_id) is not entry:
        return

    if status == "choose":
        _FLOWS[chat_id] = (instance, flow, options)
        await message.answer(f"{REPORT.title} — выберите вариант (его повторят остальные воркеры):",
                             reply_markup=_options_kb(options))
    else:
        await _finish(instance, flow, message.bot, chat_id, manager, pool)


@router.callback_query(ChoiceCB.filter(F.scope == "report_opt"))
async def rm_choose(callback: CallbackQuery, callback_data: ChoiceCB, manager: JobManager, pool: WorkerPool):
    chat_id = callback.message.chat.id
    entry = _FLOWS.get(chat_id)
    if entry is None:
        await callback.answer(STALE_BUTTON, show_alert=True)
        return

    if callback_data.value == STOP:  # ✖️: as /cancel — the slot freed, the held worker let go (_abort)
        await callback.answer()
        await manager.stop()
        await callback.message.edit_text("Жалоба отменена.", reply_markup=back_kb(ACTIVITY, manager.project_id,
                                                                                  fresh=False))
        return
    instance, flow, options = entry
    idx = int(callback_data.value)
    # concurrent updates: a double tap (or a stale keyboard) must not run a second step
    # on the same client while one is in flight
    if flow["busy"] or not 0 <= idx < len(options):
        await callback.answer()
        return
    flow["busy"] = True
    try:
        await callback.answer()
    except Exception:  # e.g. "query is too old": harmless, and must not leave busy stuck
        pass
    if _FLOWS.get(chat_id) is not entry:  # aborted during answer(): the client is no longer ours
        return

    flow["selections"].append(idx)

    try:
        status, options = await instance.step(flow, options[idx].option)
    except Exception as err:
        if _FLOWS.get(chat_id) is not entry:  # aborted meanwhile: the slot is no longer ours
            return
        _FLOWS.pop(chat_id, None)
        _free(manager, pool)  # before any await: a /cancel meanwhile must not free a foreign slot
        await release_client(flow["session"])
        await back_answer(callback.message, f"⚠️ Ошибка: {err}", ACTIVITY, manager.project_id)
        return

    if _FLOWS.get(chat_id) is not entry:
        return

    if status == "choose":
        flow["busy"] = False
        _FLOWS[chat_id] = (instance, flow, options)
        await callback.message.edit_text(f"{REPORT.title} — выберите вариант:",  # the next level
                                         reply_markup=_options_kb(options))
    else:
        _FLOWS.pop(chat_id, None)
        await _finish(instance, flow, callback.bot, chat_id, manager, pool)


STOP = "stop"  # the options' ✖️


def _options_kb(options):
    return choice_kb("report_opt", [*((option.text, str(index)) for index, option in enumerate(options)),
                                    ("✖️ Отмена", STOP)])


async def _finish(instance, flow, bot, chat_id, manager: JobManager, pool: WorkerPool):
    _FLOWS.pop(chat_id, None)
    manager.disarm_timeout()  # work starts now; don't let the inactivity timeout free the slot mid-replay
    manager.lock()            # ...nor a /cancel: replay_rest is driving the workers until release()
    stop = manager.soft_stop()  # ⏹ / /cancel skip the accounts not reached yet; _free still frees the slot
    await release_client(flow["session"])

    manager.progress = Progress()
    manager.progress.start(len(flow["rest"]) + 1)
    manager.progress.step()  # the first account already submitted while choosing
    manager.progress.ok = 1
    instance.progress = manager.progress

    # finish() drops the Progress / Stop buttons with the markup
    reporter = TelegramReporter(
        bot, chat_id, header="Жалоба…", reply_markup=stop_kb(manager.key),
        job_label=REPORT.title, workers=len(flow["rest"]) + 1,
        final_markup=back_kb(REPORT.category, manager.project_id),
        progress=manager.progress,
    )
    try:
        await reporter.start()
        await reporter("[первый аккаунт] жалоба отправлена.")
        skipped = await instance.replay_rest(
            flow["rest"], flow["peer"], flow["ids"], flow["comment"], flow["selections"], reporter, stop
        )
        # by what replay_rest skipped, not stop.is_set(): a ⏹ after the last account stopped nothing
        await reporter.finish("⏹ Остановлено" if skipped else "✅ Жалоба отправлена")
    except Exception as err:
        try:
            if reporter.message_id is not None:  # close the status message itself, like JobManager
                await reporter.finish(f"⚠️ Ошибка: {err}")
            else:
                await bot.send_message(chat_id, f"⚠️ Ошибка: {err}")
        except Exception:
            pass
    finally:
        _free(manager, pool)
