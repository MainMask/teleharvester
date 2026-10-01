import asyncio

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import ChoiceCB, FunctionCB
from bot.keyboards.common import choice_kb, yes_no_kb
from bot.routers._common import ensure_workers, resolve
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from bot.states import ChatBroadcast, Comments, Instant, PmMailing
from modules.settings import Settings

router = Router()

MODE_OPTIONS = [("Текст", "0"), ("Одиночная", "1"), ("Медиа", "2"), ("Ответ", "3"), ("Стикеры", "4")]
INSTANT_MODES = [("Текст", "0"), ("Одиночная", "1"), ("Медиа", "2"), ("Стикеры", "4")]  # reply needs a trigger
MMODE_OPTIONS = [("Админы", "admins"), ("Юзеры", "users")]
DEFAULT_TARGETS = "assets/targets.txt"


# =========================== PM mailing (with stats) ===========================

@router.callback_query(FunctionCB.filter(F.key == "pmmailing"))
async def mail_start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool):
    await callback.answer()
    if not await ensure_workers(callback, pool):
        return
    await state.set_state(PmMailing.path)
    await callback.message.answer(f"Файл получателей (.parquet/.txt), пусто = {DEFAULT_TARGETS}:")


@router.message(PmMailing.path)
async def mail_path(message: Message, state: FSMContext):
    await state.update_data(path=message.text.strip() or DEFAULT_TARGETS)
    await message.answer("Пропускать уже отправленных?", reply_markup=yes_no_kb("mail_skip"))


@router.callback_query(ChoiceCB.filter(F.scope == "mail_skip"))
async def mail_skip(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext):
    await state.update_data(skip=callback_data.value == "yes")
    await state.set_state(PmMailing.limit)
    await callback.message.answer("Сколько получателей (пусто = все):")
    await callback.answer()


@router.message(PmMailing.limit)
async def mail_limit(message: Message, state: FSMContext):
    raw = message.text.strip()
    await state.update_data(limit=int(raw) if raw.isdigit() and int(raw) > 0 else None)
    await message.answer("Прикреплять медиа?", reply_markup=yes_no_kb("mail_media"))


@router.callback_query(ChoiceCB.filter(F.scope == "mail_media"))
async def mail_media(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext):
    await state.update_data(media=callback_data.value == "yes")
    await state.set_state(PmMailing.text)
    await callback.message.answer("Текст сообщения:")
    await callback.answer()


@router.message(PmMailing.text)
async def mail_run(
    message: Message, state: FSMContext, pool: WorkerPool, functions: dict,
    manager: JobManager, settings: Settings,
):
    data = await state.get_data()
    await state.clear()

    instance, bot_function = resolve(functions, "pmmailing")

    try:
        recipients = instance.load_recipients(data["path"])
    except Exception as err:
        await message.answer(f"Файл не прочитан: {err}")
        return

    if data.get("skip"):
        instance.load_stats()
        recipients = instance.filter_unsent(recipients)

    if data.get("limit"):
        recipients = recipients[: data["limit"]]

    if not recipients:
        await message.answer("Список получателей пуст.")
        return

    text = message.text
    await manager.run(
        message.bot, message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(recipients, data.get("media", False), text, settings.delay, r),
        f"Рассылка в ЛС ({len(recipients)})…", "Рассылка завершена ✅",
    )


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
    await state.update_data(link=message.text.strip())
    await message.answer("Прикреплять медиа?", reply_markup=yes_no_kb("com_media"))


@router.callback_query(ChoiceCB.filter(F.scope == "com_media"))
async def com_media(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext):
    await state.update_data(media=callback_data.value == "yes")
    await state.set_state(Comments.text)
    await callback.message.answer("Текст комментария:")
    await callback.answer()


@router.message(Comments.text)
async def com_run(
    message: Message, state: FSMContext, pool: WorkerPool, functions: dict,
    manager: JobManager, settings: Settings,
):
    data = await state.get_data()
    await state.clear()

    instance, bot_function = resolve(functions, "comments")
    text = message.text
    await manager.run(
        message.bot, message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(data["link"], data.get("media", False), [text], settings.delay, r),
        "Рассылка в комментарии…", "Готово ✅",
    )


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
    await state.update_data(sticker_set=message.text.strip())
    await state.set_state(Instant.link)
    await message.answer("Ссылка на чат/канал:")


@router.message(Instant.link)
async def ins_run(message: Message, state: FSMContext, pool: WorkerPool, functions: dict, manager: JobManager):
    data = await state.get_data()
    await state.clear()

    instance, bot_function = resolve(functions, "instant")
    link = message.text.strip()
    await manager.run(
        message.bot, message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(
            data["choice"], data.get("mention_all", False), data.get("mention_mode"),
            data.get("sticker_set"), link, r,
        ),
        "Мгновенная рассылка…", "Готово ✅",
    )


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
async def cha_mention(
    callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext,
    pool: WorkerPool, functions: dict, manager: JobManager, settings: Settings,
):
    await state.update_data(mention_all=callback_data.value == "yes")
    await callback.answer()

    if callback_data.value == "yes":
        await callback.message.answer("Кого упоминать?", reply_markup=choice_kb("cha_mmode", MMODE_OPTIONS))
    else:
        await _chat_next(callback.message, state, pool, functions, manager, settings)


@router.callback_query(ChoiceCB.filter(F.scope == "cha_mmode"))
async def cha_mmode(
    callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext,
    pool: WorkerPool, functions: dict, manager: JobManager, settings: Settings,
):
    await state.update_data(mention_mode=callback_data.value)
    await callback.answer()
    await _chat_next(callback.message, state, pool, functions, manager, settings)


async def _chat_next(target, state, pool, functions, manager, settings):
    data = await state.get_data()
    if data.get("choice") == 4 and "sticker_set" not in data:
        await state.set_state(ChatBroadcast.sticker)
        await target.answer("Ссылка на стикерсет:")
    else:
        await _start_listener(target, state, pool, functions, manager, settings)


@router.message(ChatBroadcast.sticker)
async def cha_sticker(
    message: Message, state: FSMContext,
    pool: WorkerPool, functions: dict, manager: JobManager, settings: Settings,
):
    await state.update_data(sticker_set=message.text.strip())
    await _start_listener(message, state, pool, functions, manager, settings)


async def _start_listener(target, state, pool, functions, manager, settings):
    data = await state.get_data()
    await state.clear()

    instance, bot_function = resolve(functions, "chat")
    choice = data["choice"]
    mention_all = data.get("mention_all", False)
    mention_mode = data.get("mention_mode")
    sticker_set = data.get("sticker_set")

    def factory(func, reporter):
        broadcast = func.prepare(choice, mention_all, mention_mode, sticker_set)
        return asyncio.gather(*func.listener_coros(broadcast, reporter))

    started = await manager.run(
        target.bot, target.chat.id, pool, instance, bot_function, factory,
        "Слушатель рассылки запущен", "Слушатель остановлен",
        stop_sessions=pool.workers,
    )

    if started:
        await target.answer(
            f'Отправьте триггер "{settings.trigger}" в чат. Остановить — кнопкой ⏹ Стоп выше.'
        )
