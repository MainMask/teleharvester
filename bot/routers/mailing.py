"""📨 ЛС по базе, from a project's 📨 База и рассылка (bot/routers/projects/card.py): personal messages to a
base's people or a list, with its stats (who was mailed already, who is gone)."""
import asyncio
import os

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import ChoiceCB
from bot.keyboards.common import cancel_kb, choice_kb, yes_no_kb
from bot.routers._common import (SEND_MESSAGE_PROMPT, STALE_STEP, back_answer, build_content, busy_step, left_flow,
                                 pick_from, require_text, resolve, stale_tap)
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from bot.states import PmMailing
from functions.pmmailing import GONE_SKIPPED, PmMailingFunc
from modules.registry import BASE
from modules.settings import Settings

router = Router()

DEFAULT_TARGETS = "assets/targets.txt"
MAX_RECIPIENTS_FILE_SIZE = 5 * 1024 * 1024  # a recipients list is text; cap at 5 MB
SKIP_ASK = "Пропустить тех, кому уже писали раньше?"
LIMIT_ASK = "Сколько получателей? Пришлите число — или нажмите «♾ Все»:"


async def ask_recipients(message: Message, state: FSMContext, bases: list[tuple[str, str]]):
    """The mailing's first step, from a project's 📨 База и рассылка (bot/routers/projects/card.py): a base by button
    (`bases`: the project's), a .txt, a pasted list or a path."""
    await state.clear()  # an abandoned run's `recipients` would otherwise override a new path
    await state.set_state(PmMailing.path)
    await state.update_data(bases=[path for path, _ in bases])  # buttons carry an index into this
    choose = "выберите базу ниже, " if bases else ""
    await message.answer(
        f"📨 ЛС по базе — получатели: {choose}пришлите <b>.txt файлом</b> или вставьте список — по одному "
        "@username или номеру на строку (или путь к .parquet/.txt на сервере):",
        parse_mode="HTML",
        reply_markup=cancel_kb(PmMailing.path, choice_kb(
            "mail_base", [*((f"📄 {label}", str(i)) for i, (_, label) in enumerate(bases)),
                          (f"📄 {DEFAULT_TARGETS}", "default")])),
    )


@router.callback_query(PmMailing.path, ChoiceCB.filter(F.scope == "mail_base"))
async def mail_base(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext):
    path = DEFAULT_TARGETS if callback_data.value == "default" else \
        pick_from((await state.get_data()).get("bases", []), callback_data.value)
    if path is None:  # a button of an older list of the bases
        await stale_tap(callback)
        return
    await callback.answer()
    await state.update_data(path=path, recipients=None)  # the last choice wins: a list sent before
    await callback.message.edit_text(SKIP_ASK, reply_markup=cancel_kb(PmMailing.path, yes_no_kb("mail_skip")))


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
        recipients = PmMailingFunc.parse_list(buffer.read().decode("utf-8", "replace").splitlines())
        if not recipients:
            await message.answer("В файле нет получателей. Пришлите список заново.")
            return
        await state.update_data(recipients=recipients, path=None)
    else:
        raw = await require_text(message)  # a photo / sticker must not fall back to the default list
        if raw is None:
            return
        if raw == "-":
            await state.update_data(path=DEFAULT_TARGETS, recipients=None)
        elif "\n" not in raw and (os.path.exists(raw) or raw.endswith((".txt", ".parquet"))):
            await state.update_data(path=raw, recipients=None)  # a file path (backward compatible)
        else:
            recipients = PmMailingFunc.parse_list(raw.splitlines())
            if not recipients:
                await message.answer("Пустой список. Пришлите получателей.")
                return
            await state.update_data(recipients=recipients, path=None)

    await message.answer(SKIP_ASK, reply_markup=cancel_kb(PmMailing.path, yes_no_kb("mail_skip")))


@router.callback_query(ChoiceCB.filter(F.scope == "mail_skip"))
async def mail_skip(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext):
    if await left_flow(callback, state, PmMailing.path):
        return
    await state.update_data(skip=callback_data.value == "yes")
    await state.set_state(PmMailing.limit)
    await callback.message.edit_text(LIMIT_ASK, reply_markup=cancel_kb(PmMailing.limit, choice_kb(
        "mail_all", [("♾ Все", "all")])))
    await callback.answer()


@router.callback_query(PmMailing.limit, ChoiceCB.filter(F.scope == "mail_all"))
async def mail_all(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(limit=None)
    await state.set_state(PmMailing.message)
    await callback.message.edit_text(SEND_MESSAGE_PROMPT, reply_markup=cancel_kb(PmMailing.message))


@router.message(PmMailing.limit)
async def mail_limit(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    # only «♾ Все» (or an explicit "-", as before it) means "everyone": a typo must not turn into a mass mailing
    if raw != "-" and not (raw.isdecimal() and int(raw) > 0):
        await message.answer("Нужно положительное число — или «♾ Все» выше. Пришлите ещё раз:")
        return
    await state.update_data(limit=None if raw == "-" else int(raw))
    await state.set_state(PmMailing.message)
    await message.answer(SEND_MESSAGE_PROMPT, reply_markup=cancel_kb(PmMailing.message))


@router.message(PmMailing.message)
async def mail_run(
    message: Message, state: FSMContext, album, pool: WorkerPool, functions: dict,
    manager: JobManager, settings: Settings,
):
    # bail before reading the recipients if a job is running: it couldn't start anyway; the step stays
    if manager.active:
        await busy_step(message, manager, BASE)
        return

    data = await state.get_data()
    await state.clear()

    async def end(text):  # the run won't start: ⬅️ to where it was started
        await back_answer(message, text, BASE, manager.project_id)

    if not data.get("recipients") and not data.get("path"):  # a stale «Пропускать?» button after a state clear
        await end(STALE_STEP)
        return

    instance, bot_function = resolve(functions, "pmmailing")

    if data.get("recipients"):  # list supplied inline or via an uploaded .txt
        recipients = list(data["recipients"])
    else:
        try:
            # off the event loop: a .parquet recipients DB can be large and load_recipients
            # is blocking (pq.read_table), which would otherwise stall the bot's polling loop
            recipients = await asyncio.to_thread(instance.load_recipients, data["path"])
        except Exception as err:
            await end(f"Файл не прочитан: {err}")
            return

    def drop_gone(rows):  # the gone ones out before the limit: they take no place in it
        instance.load_stats()
        return instance.drop_gone(rows)

    before = len(recipients)
    try:  # off the event loop, as the base: the stats grow with every recipient ever mailed
        recipients = await asyncio.to_thread(drop_gone, recipients)
    except Exception as err:
        await end(f"Статистика не прочитана: {err}")
        return
    if gone := before - len(recipients):
        await message.answer(f"{GONE_SKIPPED}: {gone}")
    if data.get("skip"):
        recipients = instance.filter_unsent(recipients)

    if data.get("limit"):
        recipients = recipients[: data["limit"]]

    if not recipients:
        await end("Список получателей пуст.")
        return

    content = await build_content(message, album)
    if content is None:  # the run's data is read again with the next message: the step comes back
        await state.set_state(PmMailing.message)
        await state.update_data(**data)
        return

    await manager.run(
        message.bot, message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(recipients, content, None, r),
        f"Рассылка в ЛС ({len(recipients)})…", cleanup=content.cleanup,
    )
