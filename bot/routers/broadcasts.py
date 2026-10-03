import asyncio

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import ChoiceCB, FunctionCB
from bot.keyboards.common import choice_kb, yes_no_kb
from bot.routers._common import SEND_MESSAGE_PROMPT, build_content, ensure_workers, require_text, resolve
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from bot.states import ChatBroadcast, Comments, Instant, PmMailing
from modules.settings import Settings

router = Router()

MODE_OPTIONS = [("Текст", "0"), ("Медиа", "2"), ("Ответ", "3"), ("Стикеры", "4")]
INSTANT_MODES = [("Текст", "0"), ("Медиа", "2"), ("Стикеры", "4")]  # reply needs a trigger
MMODE_OPTIONS = [("Админы", "admins"), ("Юзеры", "users")]
DEFAULT_TARGETS = "assets/targets.txt"


# =========================== PM mailing (with stats) ===========================

@router.callback_query(FunctionCB.filter(F.key == "pmmailing"))
async def mail_start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool):
    await callback.answer()
    if not await ensure_workers(callback, pool):
        return
    await state.set_state(PmMailing.path)
    await callback.message.answer(f"Файл получателей (.parquet/.txt), «-» = {DEFAULT_TARGETS}:")


@router.message(PmMailing.path)
async def mail_path(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    await state.update_data(path=DEFAULT_TARGETS if raw in ("", "-") else raw)  # Telegram can't send ""
    await message.answer("Пропускать уже отправленных?", reply_markup=yes_no_kb("mail_skip"))


@router.callback_query(ChoiceCB.filter(F.scope == "mail_skip"))
async def mail_skip(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext):
    await state.update_data(skip=callback_data.value == "yes")
    await state.set_state(PmMailing.limit)
    await callback.message.answer("Сколько получателей («-» = все):")
    await callback.answer()


@router.message(PmMailing.limit)
async def mail_limit(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    # only an explicit "-" means "everyone": a typo must not turn into a mass mailing
    if raw != "-" and not (raw.isdecimal() and int(raw) > 0):
        await message.answer("Введите положительное число («-» = все):")
        return
    await state.update_data(limit=None if raw == "-" else int(raw))
    await state.set_state(PmMailing.message)
    await message.answer(SEND_MESSAGE_PROMPT)


@router.message(PmMailing.message)
async def mail_run(
    message: Message, state: FSMContext, album, pool: WorkerPool, functions: dict,
    manager: JobManager, settings: Settings,
):
    # bail before touching the (shared, singleton) function instance if a job is running
    if manager.active:
        await message.answer(f"⛔ Занят: {manager.label}. Остановите текущую задачу.")
        await state.clear()
        return

    data = await state.get_data()
    await state.clear()

    instance, bot_function = resolve(functions, "pmmailing")

    try:
        # off the event loop: a .parquet recipients DB can be large and load_recipients
        # is blocking (pq.read_table), which would otherwise stall the bot's polling loop
        recipients = await asyncio.to_thread(instance.load_recipients, data["path"])
    except Exception as err:
        await message.answer(f"Файл не прочитан: {err}")
        return

    if data.get("skip"):
        # re-check after the await above: a mailing started meanwhile owns instance.stats
        # (its run() loaded it), and reloading here would drop its unsaved records
        if manager.active:
            await message.answer(f"⛔ Занят: {manager.label}. Остановите текущую задачу.")
            return
        try:
            instance.load_stats()
        except Exception as err:
            await message.answer(f"Статистика не прочитана: {err}")
            return
        recipients = instance.filter_unsent(recipients)

    if data.get("limit"):
        recipients = recipients[: data["limit"]]

    if not recipients:
        await message.answer("Список получателей пуст.")
        return

    content = await build_content(message, album)
    if content is None:
        return

    async def job(func, reporter):
        try:
            await func.run(recipients, content, settings.delay, reporter)
        finally:
            content.cleanup()

    started = await manager.run(
        message.bot, message.chat.id, pool, instance, bot_function, job,
        f"Рассылка в ЛС ({len(recipients)})…", "Рассылка завершена ✅",
    )
    if not started:
        content.cleanup()


# =========================== Broadcast to comments ===========================

@router.callback_query(FunctionCB.filter(F.key == "comments"))
async def com_start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool):
    await callback.answer()
    if not await ensure_workers(callback, pool):
        return
    await state.set_state(Comments.link)
    await callback.message.answer("Ссылка на пост:")


@router.message(Comments.link)
async def com_link(message: Message, state: FSMContext):
    link = await require_text(message)
    if link is None:
        return
    await state.update_data(link=link)
    await state.set_state(Comments.message)
    await message.answer(SEND_MESSAGE_PROMPT)


@router.message(Comments.message)
async def com_run(
    message: Message, state: FSMContext, album, pool: WorkerPool, functions: dict,
    manager: JobManager, settings: Settings,
):
    data = await state.get_data()
    await state.clear()

    content = await build_content(message, album)
    if content is None:
        return

    instance, bot_function = resolve(functions, "comments")

    async def job(func, reporter):
        try:
            await func.run(data["link"], content, settings.delay, reporter)
        finally:
            content.cleanup()

    started = await manager.run(
        message.bot, message.chat.id, pool, instance, bot_function, job,
        "Рассылка в комментарии…", "Готово ✅",
    )
    if not started:
        content.cleanup()


# =========================== Instant broadcast ===========================

@router.callback_query(FunctionCB.filter(F.key == "instant"))
async def ins_start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool):
    await callback.answer()
    if not await ensure_workers(callback, pool):
        return
    await state.clear()
    await callback.message.answer("Режим рассылки:", reply_markup=choice_kb("ins_mode", INSTANT_MODES))


@router.callback_query(ChoiceCB.filter(F.scope == "ins_mode"))
async def ins_mode(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext):
    await state.update_data(choice=int(callback_data.value))
    await callback.message.answer("Упоминать всех?", reply_markup=yes_no_kb("ins_mention"))
    await callback.answer()


@router.callback_query(ChoiceCB.filter(F.scope == "ins_mention"))
async def ins_mention(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext):
    await state.update_data(mention_all=callback_data.value == "yes")
    await callback.answer()

    if callback_data.value == "yes":
        await callback.message.answer("Кого упоминать?", reply_markup=choice_kb("ins_mmode", MMODE_OPTIONS))
    else:
        await _instant_next(callback.message, state)


@router.callback_query(ChoiceCB.filter(F.scope == "ins_mmode"))
async def ins_mmode(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext):
    await state.update_data(mention_mode=callback_data.value)
    await callback.answer()
    await _instant_next(callback.message, state)


async def _instant_next(target, state: FSMContext):
    data = await state.get_data()
    if data.get("choice") == 4 and "sticker_set" not in data:
        await state.set_state(Instant.sticker)
        await target.answer("Ссылка на стикерсет:")
    else:
        await state.set_state(Instant.link)
        await target.answer("Ссылка на чат/канал:")


@router.message(Instant.sticker)
async def ins_sticker(message: Message, state: FSMContext):
    sticker_set = await require_text(message)
    if sticker_set is None:
        return
    await state.update_data(sticker_set=sticker_set)
    await state.set_state(Instant.link)
    await message.answer("Ссылка на чат/канал:")


@router.message(Instant.link)
async def ins_link(message: Message, state: FSMContext):
    link = await require_text(message)
    if link is None:
        return
    await state.update_data(link=link)
    await state.set_state(Instant.message)
    await message.answer(SEND_MESSAGE_PROMPT)


@router.message(Instant.message)
async def ins_run(
    message: Message, state: FSMContext, album,
    pool: WorkerPool, functions: dict, manager: JobManager,
):
    data = await state.get_data()
    await state.clear()

    if "choice" not in data or "link" not in data:
        await message.answer("Флоу устарел, начните заново.")
        return

    content = await build_content(message, album)
    if content is None:
        return

    instance, bot_function = resolve(functions, "instant")

    async def job(func, reporter):
        try:
            await func.run(
                data["choice"], data.get("mention_all", False), data.get("mention_mode"),
                data.get("sticker_set"), content, data["link"], reporter,
            )
        finally:
            content.cleanup()

    started = await manager.run(
        message.bot, message.chat.id, pool, instance, bot_function, job,
        "Мгновенная рассылка…", "Готово ✅",
    )
    if not started:
        content.cleanup()


# =========================== Broadcast to chat (trigger) ===========================

@router.callback_query(FunctionCB.filter(F.key == "chat"))
async def cha_start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool):
    await callback.answer()
    if not await ensure_workers(callback, pool):
        return
    await state.clear()
    await callback.message.answer("Режим кампании:", reply_markup=choice_kb("cha_mode", MODE_OPTIONS))


@router.callback_query(ChoiceCB.filter(F.scope == "cha_mode"))
async def cha_mode(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext):
    await state.update_data(choice=int(callback_data.value))
    await callback.message.answer("Упоминать всех?", reply_markup=yes_no_kb("cha_mention"))
    await callback.answer()


@router.callback_query(ChoiceCB.filter(F.scope == "cha_mention"))
async def cha_mention(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext):
    await state.update_data(mention_all=callback_data.value == "yes")
    await callback.answer()

    if callback_data.value == "yes":
        await callback.message.answer("Кого упоминать?", reply_markup=choice_kb("cha_mmode", MMODE_OPTIONS))
    else:
        await _chat_next(callback.message, state)


@router.callback_query(ChoiceCB.filter(F.scope == "cha_mmode"))
async def cha_mmode(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext):
    await state.update_data(mention_mode=callback_data.value)
    await callback.answer()
    await _chat_next(callback.message, state)


async def _chat_next(target, state: FSMContext):
    data = await state.get_data()
    if data.get("choice") == 4 and "sticker_set" not in data:
        await state.set_state(ChatBroadcast.sticker)
        await target.answer("Ссылка на стикерсет:")
    else:
        await state.set_state(ChatBroadcast.message)
        await target.answer(SEND_MESSAGE_PROMPT)


@router.message(ChatBroadcast.sticker)
async def cha_sticker(message: Message, state: FSMContext):
    sticker_set = await require_text(message)
    if sticker_set is None:
        return
    await state.update_data(sticker_set=sticker_set)
    await state.set_state(ChatBroadcast.message)
    await message.answer(SEND_MESSAGE_PROMPT)


@router.message(ChatBroadcast.message)
async def cha_run(
    message: Message, state: FSMContext, album,
    pool: WorkerPool, functions: dict, manager: JobManager, settings: Settings,
):
    data = await state.get_data()
    await state.clear()

    if "choice" not in data:
        await message.answer("Флоу устарел, начните заново.")
        return

    content = await build_content(message, album)
    if content is None:
        return

    instance, bot_function = resolve(functions, "chat")

    choice = data["choice"]
    mention_all = data.get("mention_all", False)
    mention_mode = data.get("mention_mode")
    sticker_set = data.get("sticker_set")

    async def factory(func, reporter):
        broadcast = func.prepare(choice, mention_all, mention_mode, sticker_set, content)
        try:
            await asyncio.gather(*func.listener_coros(broadcast, reporter))
        finally:
            content.cleanup()

    started = await manager.run(
        message.bot, message.chat.id, pool, instance, bot_function, factory,
        "Слушатель рассылки запущен", "Слушатель остановлен",
        stop_sessions=pool.workers,
    )

    if started:
        await message.answer(
            f'Отправьте триггер "{settings.trigger}" в чат. Остановить — кнопкой ⏹ Стоп выше.'
        )
    else:
        content.cleanup()
