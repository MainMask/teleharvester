from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import ChoiceCB, FunctionCB
from bot.routers._common import ensure_workers, require_text, resolve
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from bot.services.runner import TelegramReporter
from bot.states import ReportMessage, ReportUser

router = Router()

# chat_id -> (instance, flow-state dict, current options list) for the dynamic report flow
_FLOWS: dict[int, tuple] = {}


# =========================== report (user) ===========================

@router.callback_query(FunctionCB.filter(F.key == "reportuser"))
async def ru_start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool):
    await callback.answer()
    if not await ensure_workers(callback, pool):
        return
    await state.set_state(ReportUser.username)
    await callback.message.answer("Username/ссылка на пользователя:")


@router.message(ReportUser.username)
async def ru_username(message: Message, state: FSMContext, functions: dict):
    username = await require_text(message)
    if username is None:
        return
    await state.update_data(username=username)

    instance, _ = resolve(functions, "reportuser")
    builder = InlineKeyboardBuilder()
    for index, (label, _reason) in enumerate(instance.reasons):
        builder.button(text=label, callback_data=ChoiceCB(scope="reason", value=str(index)))
    builder.adjust(1)

    await message.answer("Причина:", reply_markup=builder.as_markup())


@router.callback_query(ChoiceCB.filter(F.scope == "reason"))
async def ru_reason(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext):
    await state.update_data(reason_index=int(callback_data.value))
    await state.set_state(ReportUser.comment)
    await callback.message.answer("Комментарий (можно пустой — отправьте «-»):")
    await callback.answer()


@router.message(ReportUser.comment)
async def ru_run(message: Message, state: FSMContext, pool: WorkerPool, functions: dict, manager: JobManager):
    if not message.text:
        await message.answer("Ожидается текст (или «-» для пустого).")
        return

    data = await state.get_data()
    await state.clear()

    if "username" not in data or "reason_index" not in data:  # stale flow after a state clear
        await message.answer("Флоу устарел, начните заново.")
        return

    comment = "" if message.text.strip() == "-" else message.text
    instance, bot_function = resolve(functions, "reportuser")
    reason_type = instance.reasons[data["reason_index"]][1]

    await manager.run(
        message.bot, message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(data["username"], reason_type, comment, r),
        "Репорт пользователя…", "Готово ✅",
    )


# ====================== report (message/post) ======================

async def _abort(chat_id: int):
    """on_abort for the manager: disconnect the held worker and drop the flow."""
    entry = _FLOWS.pop(chat_id, None)
    if entry is not None:
        try:
            await entry[1]["session"].disconnect()
        except Exception:
            pass


@router.callback_query(FunctionCB.filter(F.key == "report"))
async def rm_start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool):
    await callback.answer()
    if not await ensure_workers(callback, pool):
        return
    await state.set_state(ReportMessage.link)
    await callback.message.answer("Ссылка/peer поста:")


@router.message(ReportMessage.link)
async def rm_link(message: Message, state: FSMContext):
    link = await require_text(message)
    if link is None:
        return
    await state.update_data(link=link)
    await state.set_state(ReportMessage.ids)
    await message.answer("ID постов через запятую (напр. 12,13,14):")


@router.message(ReportMessage.ids)
async def rm_ids(message: Message, state: FSMContext):
    parts = [p.strip() for p in (message.text or "").split(",") if p.strip()]
    if not parts or not all(p.isdecimal() for p in parts):
        await message.answer("Введите числовые id через запятую:")
        return
    await state.update_data(ids=[int(p) for p in parts])
    await state.set_state(ReportMessage.comment)
    await message.answer("Комментарий (можно «-»):")


@router.message(ReportMessage.comment)
async def rm_begin(message: Message, state: FSMContext, pool: WorkerPool, functions: dict, manager: JobManager):
    if not message.text:
        await message.answer("Ожидается текст (или «-» для пустого).")
        return

    data = await state.get_data()
    await state.clear()

    chat_id = message.chat.id

    if not manager.acquire("Репорт", on_abort=lambda: _abort(chat_id), timeout=600):
        await message.answer(f"⛔ Занят: {manager.label}. Остановите текущую задачу.")
        return

    comment = "" if message.text.strip() == "-" else message.text
    instance, _ = resolve(functions, "report")
    workers = pool.delegate(instance)

    if not workers:  # guard: never index into an empty pool with the slot held
        manager.release()
        await message.answer("Нет воркеров.")
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
        await first.connect()
        if _FLOWS.get(chat_id) is not entry:  # cancelled during connect: send no report
            try:
                await first.disconnect()
            except Exception:
                pass
            return
        status, options = await instance.step(flow, b"")
    except Exception as err:
        if _FLOWS.get(chat_id) is not entry:  # aborted meanwhile: the slot is no longer ours
            return
        _FLOWS.pop(chat_id, None)
        manager.release()  # before any await: a /cancel meanwhile must not free a foreign slot
        try:
            await first.disconnect()
        except Exception:
            pass
        await message.answer(f"Ошибка: {err}")
        return

    if _FLOWS.get(chat_id) is not entry:
        return

    if status == "choose":
        _FLOWS[chat_id] = (instance, flow, options)
        await message.answer("Выберите вариант:", reply_markup=_options_kb(options))
    else:
        await _finish(instance, flow, message.bot, chat_id, manager)


@router.callback_query(ChoiceCB.filter(F.scope == "report_opt"))
async def rm_choose(callback: CallbackQuery, callback_data: ChoiceCB, manager: JobManager):
    chat_id = callback.message.chat.id
    entry = _FLOWS.get(chat_id)
    if entry is None:
        await callback.answer("Флоу устарел, начните заново.", show_alert=True)
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
        manager.release()  # before any await: a /cancel meanwhile must not free a foreign slot
        try:
            await flow["session"].disconnect()
        except Exception:
            pass
        await callback.message.answer(f"Ошибка: {err}")
        return

    if _FLOWS.get(chat_id) is not entry:
        return

    if status == "choose":
        flow["busy"] = False
        _FLOWS[chat_id] = (instance, flow, options)
        await callback.message.answer("Выберите вариант:", reply_markup=_options_kb(options))
    else:
        _FLOWS.pop(chat_id, None)
        await _finish(instance, flow, callback.bot, chat_id, manager)


def _options_kb(options):
    builder = InlineKeyboardBuilder()
    for index, option in enumerate(options):
        builder.button(text=option.text, callback_data=ChoiceCB(scope="report_opt", value=str(index)))
    builder.adjust(1)
    return builder.as_markup()


async def _finish(instance, flow, bot, chat_id, manager: JobManager):
    _FLOWS.pop(chat_id, None)
    manager.disarm_timeout()  # work starts now; don't let the inactivity timeout free the slot mid-replay
    manager.lock()            # ...nor a /cancel: replay_rest is driving the workers until release()
    try:
        await flow["session"].disconnect()
    except Exception:
        pass

    reporter = TelegramReporter(bot, chat_id, header="Репорт…")
    try:
        await reporter.start()
        await reporter("[первый аккаунт] submitted.")
        await instance.replay_rest(
            flow["rest"], flow["peer"], flow["ids"], flow["comment"], flow["selections"], reporter
        )
        await reporter.finish("Репорт отправлен ✅")
    except Exception as err:
        try:
            if reporter.message_id is not None:  # close the status message itself, like JobManager
                await reporter.finish(f"⚠️ Ошибка: {err}")
            else:
                await bot.send_message(chat_id, f"⚠️ Ошибка: {err}")
        except Exception:
            pass
    finally:
        manager.release()
