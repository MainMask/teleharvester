import asyncio
import os

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import ChoiceCB, FunctionCB
from bot.keyboards.common import choice_kb, yes_no_kb
from bot.routers._common import SEND_MESSAGE_PROMPT, build_content, ensure_workers, require_text, resolve
from bot.services.delegation import WorkerPool
from modules import scraped_files
from bot.services.jobs import JobManager
from bot.states import BroadcastOptions, ChatBroadcast, Comments, Instant, PmMailing
from modules.settings import Settings

router = Router()

MODE_OPTIONS = [("Текст", "0"), ("Медиа", "2"), ("Ответ", "3"), ("Стикеры", "4")]
INSTANT_MODES = [("Текст", "0"), ("Медиа", "2"), ("Стикеры", "4")]  # reply needs a trigger
MMODE_OPTIONS = [("Админы", "admins"), ("Юзеры", "users")]
DEFAULT_TARGETS = "assets/targets.txt"
MAX_RECIPIENTS_FILE_SIZE = 5 * 1024 * 1024  # a recipients list is text; cap at 5 MB


def _parse_recipients(text: str) -> list[str]:
    """One recipient per line (@username / phone); blank lines dropped."""
    return [line.strip() for line in text.splitlines() if line.strip()]


# =========================== PM mailing (with stats) ===========================

@router.callback_query(FunctionCB.filter(F.key == "pmmailing"))
async def mail_start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool):
    await callback.answer()
    if not await ensure_workers(callback, pool):
        return
    await state.clear()  # an abandoned run's `recipients` would otherwise override a new path
    await state.set_state(PmMailing.path)
    bases = scraped_files.participant_bases()
    await state.update_data(bases=[path for path, _ in bases])  # buttons carry an index into this
    choose = "выберите базу из скрапа ниже, или " if bases else ""
    await callback.message.answer(
        f"Получатели: {choose}пришлите <b>.txt файлом</b>, вставьте список (по одному @username/номеру "
        f"на строку), или укажите путь к .parquet/.txt («-» = {DEFAULT_TARGETS}):",
        parse_mode="HTML",
        reply_markup=choice_kb("mail_base", [(label, str(i)) for i, (_, label) in enumerate(bases)])
        if bases else None,
    )


@router.callback_query(PmMailing.path, ChoiceCB.filter(F.scope == "mail_base"))
async def mail_base(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext):
    await callback.answer()
    bases = (await state.get_data()).get("bases", [])
    index = int(callback_data.value)
    if not 0 <= index < len(bases):
        return
    await state.update_data(path=bases[index])
    await callback.message.answer("Пропускать уже отправленных?", reply_markup=yes_no_kb("mail_skip"))


@router.message(PmMailing.path)
async def mail_path(message: Message, state: FSMContext):
    if message.document is not None:
        name = (message.document.file_name or "").lower()
        if name.endswith(".parquet"):
            await message.answer("Для .parquet укажите путь к файлу (не загрузкой). Пришлите путь или .txt.")
            return
        if (message.document.file_size or 0) > MAX_RECIPIENTS_FILE_SIZE:
            await message.answer("Файл слишком большой. Пришлите .txt поменьше или укажите путь.")
            return
        buffer = await message.bot.download(message.document)
        recipients = _parse_recipients(buffer.read().decode("utf-8", "replace"))
        if not recipients:
            await message.answer("В файле нет получателей. Пришлите список заново.")
            return
        await state.update_data(recipients=recipients)
    else:
        raw = await require_text(message)  # a photo / sticker must not fall back to the default list
        if raw is None:
            return
        if raw == "-":
            await state.update_data(path=DEFAULT_TARGETS)
        elif "\n" not in raw and (os.path.exists(raw) or raw.endswith((".txt", ".parquet"))):
            await state.update_data(path=raw)  # a file path (backward compatible)
        else:
            recipients = _parse_recipients(raw)
            if not recipients:
                await message.answer("Пустой список. Пришлите получателей.")
                return
            await state.update_data(recipients=recipients)

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

    if "recipients" not in data and "path" not in data:  # a stale «Пропускать?» button after a state clear
        await message.answer("Флоу устарел, начните заново.")
        return

    instance, bot_function = resolve(functions, "pmmailing")

    if "recipients" in data:  # list supplied inline or via an uploaded .txt
        recipients = list(data["recipients"])
    else:
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
        instance.stats = {}  # run() reloads it; the singleton must not keep the ledger if the job never starts

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
            # every run (and "skip already sent") reloads the ledger from its file: the shared
            # instance must not hold everyone ever messaged in memory between mailings
            func.stats = {}

    started = await manager.run(
        message.bot, message.chat.id, pool, instance, bot_function, job,
        f"Рассылка в ЛС ({len(recipients)})…", "Рассылка завершена ✅",
        cleanup=content.cleanup,
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
async def com_link(message: Message, state: FSMContext, settings: Settings):
    link = await require_text(message)
    if link is None:
        return
    await state.update_data(link=link)
    await _ask_count(message, state, settings, "comments")


@router.message(Comments.message)
async def com_run(
    message: Message, state: FSMContext, album, pool: WorkerPool, functions: dict,
    manager: JobManager, settings: Settings,
):
    data = await state.get_data()
    await state.clear()

    if "link" not in data or "messages_count" not in data:
        await message.answer("Флоу устарел, начните заново.")
        return

    content = await build_content(message, album)
    if content is None:
        return
    if not await _save_options(message, data, settings, manager, content):
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
        cleanup=content.cleanup,
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
async def ins_link(message: Message, state: FSMContext, settings: Settings):
    link = await require_text(message)
    if link is None:
        return
    await state.update_data(link=link)
    await _ask_count(message, state, settings, "instant")


@router.message(Instant.message)
async def ins_run(
    message: Message, state: FSMContext, album,
    pool: WorkerPool, functions: dict, manager: JobManager, settings: Settings,
):
    data = await state.get_data()
    await state.clear()

    if "choice" not in data or "link" not in data or "messages_count" not in data:
        await message.answer("Флоу устарел, начните заново.")
        return

    content = await build_content(message, album)
    if content is None:
        return
    if not await _save_options(message, data, settings, manager, content):
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
        cleanup=content.cleanup,
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
async def cha_mention(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext, settings: Settings):
    await state.update_data(mention_all=callback_data.value == "yes")
    await callback.answer()

    if callback_data.value == "yes":
        await callback.message.answer("Кого упоминать?", reply_markup=choice_kb("cha_mmode", MMODE_OPTIONS))
    else:
        await _chat_next(callback.message, state, settings)


@router.callback_query(ChoiceCB.filter(F.scope == "cha_mmode"))
async def cha_mmode(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext, settings: Settings):
    await state.update_data(mention_mode=callback_data.value)
    await callback.answer()
    await _chat_next(callback.message, state, settings)


async def _chat_next(target, state: FSMContext, settings: Settings):
    data = await state.get_data()
    if data.get("choice") == 4 and "sticker_set" not in data:
        await state.set_state(ChatBroadcast.sticker)
        await target.answer("Ссылка на стикерсет:")
    else:
        await _ask_trigger(target, state, settings)


@router.message(ChatBroadcast.sticker)
async def cha_sticker(message: Message, state: FSMContext, settings: Settings):
    sticker_set = await require_text(message)
    if sticker_set is None:
        return
    await state.update_data(sticker_set=sticker_set)
    await _ask_trigger(message, state, settings)


@router.message(ChatBroadcast.message)
async def cha_run(
    message: Message, state: FSMContext, album,
    pool: WorkerPool, functions: dict, manager: JobManager, settings: Settings,
):
    data = await state.get_data()
    await state.clear()

    # no trigger: an empty one would fire on every media message without a caption
    if "choice" not in data or not data.get("trigger") or "messages_count" not in data:
        await message.answer("Флоу устарел, начните заново.")
        return

    content = await build_content(message, album)
    if content is None:
        return
    if not await _save_options(message, data, settings, manager, content):
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
        stop_sessions=pool.workers, cleanup=content.cleanup,
    )

    if started:
        await message.answer(
            f'Отправьте триггер "{settings.trigger}" в чат. Остановить — кнопкой ⏹ Стоп выше.'
        )
    else:
        content.cleanup()


# ================= Shared steps: the trigger and the messages count =================
# Chosen per run, with the current config value one tap away; written to config.toml only
# when the job really starts (_save_options), so a run refused for a busy slot changes nothing
# and a running job — which reads settings live — is never switched mid-way.

_MESSAGE_STEPS = {"chat": ChatBroadcast.message, "instant": Instant.message, "comments": Comments.message}


def _count_text(count: int) -> str:
    return "без лимита" if count == 0 else str(count)


async def _ask_trigger(target, state: FSMContext, settings: Settings):
    await state.set_state(BroadcastOptions.trigger)
    if not settings.trigger:  # nothing to keep: an empty trigger would fire on any caption-less media
        await target.answer("Триггер — текст, после которого воркеры начнут писать в чат. Пришлите его:")
        return
    await target.answer(
        f"Триггер — текст, после которого воркеры начнут писать в чат. Сейчас: «{settings.trigger}».\n"
        "Оставьте его или пришлите новый:",
        reply_markup=choice_kb("bc_trigger", [(f"✅ Оставить «{settings.trigger[:40]}»", "keep")]),
    )


async def _ask_count(target, state: FSMContext, settings: Settings, flow: str):
    await state.update_data(flow=flow)
    await state.set_state(BroadcastOptions.count)
    await target.answer(
        f"Сколько сообщений отправит каждый воркер? Сейчас: {_count_text(settings.messages_count)}.\n"
        "Пришлите число (0 = без лимита, до ⏹) или оставьте текущее:",
        reply_markup=choice_kb("bc_count", [(f"✅ Оставить: {_count_text(settings.messages_count)}", "keep")]),
    )


async def _to_message_step(target, state: FSMContext):
    flow = (await state.get_data()).get("flow")
    if flow not in _MESSAGE_STEPS:  # a stale button after the state was cleared
        await state.clear()
        await target.answer("Флоу устарел, начните заново.")
        return
    await state.set_state(_MESSAGE_STEPS[flow])
    await target.answer(SEND_MESSAGE_PROMPT)


@router.callback_query(BroadcastOptions.trigger, ChoiceCB.filter(F.scope == "bc_trigger"))
async def trigger_keep(callback: CallbackQuery, state: FSMContext, settings: Settings):
    await callback.answer()
    if not settings.trigger:
        return
    await state.update_data(trigger=settings.trigger)
    await _ask_count(callback.message, state, settings, "chat")


@router.message(BroadcastOptions.trigger)
async def trigger_input(message: Message, state: FSMContext, settings: Settings):
    trigger = await require_text(message)
    if trigger is None:
        return
    await state.update_data(trigger=trigger)
    await _ask_count(message, state, settings, "chat")


@router.callback_query(BroadcastOptions.count, ChoiceCB.filter(F.scope == "bc_count"))
async def count_keep(callback: CallbackQuery, state: FSMContext, settings: Settings):
    await callback.answer()
    await state.update_data(messages_count=settings.messages_count)
    await _to_message_step(callback.message, state)


@router.message(BroadcastOptions.count)
async def count_input(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if not raw.isdecimal():
        await message.answer("Нужно целое число: сколько сообщений от каждого воркера (0 = без лимита).")
        return
    await state.update_data(messages_count=int(raw))
    await _to_message_step(message, state)


async def _save_options(message: Message, data: dict, settings: Settings, manager: JobManager, content) -> bool:
    """Write the run's trigger / messages count to config.toml right before it starts; False
    (content dropped, operator told) if it can't start. No await follows the busy check, so
    the caller's manager.run takes the slot this saw free."""
    if manager.active:
        content.cleanup()
        await message.answer(f"⛔ Занят: {manager.label}. Остановите текущую задачу.")
        return False
    try:
        if "trigger" in data and data["trigger"] != settings.trigger:
            settings.set_trigger(data["trigger"])
        if data["messages_count"] != settings.messages_count:
            settings.set_messages_count(data["messages_count"])
    except (OSError, ValueError) as err:  # ValueError: a config.toml broken by hand meanwhile
        content.cleanup()
        await message.answer(f"⚠️ Не удалось сохранить config.toml: {err}")
        return False
    return True
