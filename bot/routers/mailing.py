"""📨 Рассылка, from a project's 🎯 База и рассылка (bot/routers/projects/card.py): personal messages to a
base's people or a list, with its stats (who was mailed already, who is gone)."""
import asyncio
import os

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import ChoiceCB
from bot.keyboards.common import cancel_kb, choice_kb, yes_no_kb
from bot.routers._common import (SEND_MESSAGE_PROMPT, STALE_STEP, back_answer, build_content, busy_step, download,
                                 left_flow, pick_from, require_text, resolve, stale_tap)
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from bot.states import PmMailing
from functions.pmmailing import GONE_SKIPPED, SKIP_ASK, PmMailingFunc
from modules import outreach, project_base
from modules.registry import BASE, HYPOTHESES, title
from modules.settings import Settings

router = Router()

DEFAULT_TARGETS = "assets/targets.txt"
MAX_RECIPIENTS_FILE_SIZE = 5 * 1024 * 1024  # a recipients list is text; cap at 5 MB
LIMIT_ASK = "Сколько получателей? Пришлите число или нажмите «♾ Все»:"
HYPOTHESIS_ASK = ("🧪 К какой гипотезе относится эта рассылка? Выберите ниже или пришлите новую одним сообщением, "
                  "например: «Если начать с вопроса о канале, ответят 8% вместо 5%, потому что это не похоже на "
                  f"спам». Цифры по гипотезам — в «{HYPOTHESES}».")
NO_HYPOTHESIS_VALUE = "none"  # «Без гипотезы»'s button
SHOWN_HYPOTHESIS = 50  # of a hypothesis's text, on its button
HYPOTHESIS_BUTTONS = 20  # the newest open ones: the older ones are closed in 🧪 Гипотезы


async def ask_recipients(message: Message, state: FSMContext, project: str, whole: int, bases: list[tuple[str, str]]):
    """The mailing's first step, from a project's 🎯 База и рассылка (bot/routers/projects/card.py): 📚 its whole
    base (`whole` people, each once: project_base) or one of its files (`bases`) by button, a .txt, a pasted list or
    a path."""
    await state.clear()  # an abandoned run's `recipients` would otherwise override a new path
    await state.set_state(PmMailing.path)
    await state.update_data(bases=[path for path, _ in bases], project=project)  # buttons carry an index into this
    await message.edit_text(  # in 🎯 База и рассылка's place, as a choice by button
        f"{title('pmmailing')} — выберите базу ниже, пришлите .txt файлом или вставьте список — по одному "
        "@username, ссылке t.me или номеру на строку (или путь к .parquet/.txt на сервере):",
        reply_markup=cancel_kb(PmMailing.path, choice_kb(
            "mail_base", [*([(f"📚 Вся база · {whole}", project_base.WHOLE)] if bases else []),
                          *((f"📄 {label}", str(i)) for i, (_, label) in enumerate(bases)),
                          (f"📄 {DEFAULT_TARGETS}", "default")])),
    )


@router.callback_query(PmMailing.path, ChoiceCB.filter(F.scope == "mail_base"))
async def mail_base(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext):
    data = await state.get_data()
    if callback_data.value == project_base.WHOLE:  # 📚: the project's base, read when the run starts
        path = project_base.WHOLE if data.get("project") else None
    elif callback_data.value == "default":
        path = DEFAULT_TARGETS
    else:
        path = pick_from(data.get("bases", []), callback_data.value)
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
            await message.answer("Для .parquet нужен путь к файлу на сервере, не загрузка. Пришлите ещё раз:")
            return
        if (message.document.file_size or 0) > MAX_RECIPIENTS_FILE_SIZE:
            await message.answer("Файл слишком большой. Пришлите .txt поменьше или путь к нему:")
            return
        if (data := await download(message, message.document)) is None:
            return
        recipients = PmMailingFunc.parse_list(data.decode("utf-8", "replace").splitlines())
        if not recipients:
            await message.answer("В файле нет получателей. Пришлите ещё раз:")
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
                await message.answer("Пустой список. Пришлите ещё раз:")
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


def _hypothesis_kb(pool: WorkerPool):
    """The project's open hypotheses (modules/outreach.py), newest first, then «Без гипотезы»."""
    hypotheses = outreach.open_hypotheses(pool.storage.home)[::-1][:HYPOTHESIS_BUTTONS]
    options = [(f"#{h['id']} {h['text'][:SHOWN_HYPOTHESIS]}", str(h["id"])) for h in hypotheses]
    options.append((outreach.NO_HYPOTHESIS, NO_HYPOTHESIS_VALUE))
    return cancel_kb(PmMailing.hypothesis, choice_kb("mail_hyp", options))


@router.callback_query(PmMailing.limit, ChoiceCB.filter(F.scope == "mail_all"))
async def mail_all(callback: CallbackQuery, state: FSMContext, pool: WorkerPool):
    await callback.answer()
    await state.update_data(limit=None)
    await state.set_state(PmMailing.hypothesis)
    await callback.message.edit_text(HYPOTHESIS_ASK, reply_markup=_hypothesis_kb(pool))


@router.message(PmMailing.limit)
async def mail_limit(message: Message, state: FSMContext, pool: WorkerPool):
    raw = (message.text or "").strip()
    # only «♾ Все» (or an explicit "-", as before it) means "everyone": a typo must not turn into a mass mailing
    if raw != "-" and not (raw.isdecimal() and int(raw) > 0):
        await message.answer("Нужно положительное число — или «♾ Все» выше. Пришлите ещё раз:")
        return
    await state.update_data(limit=None if raw == "-" else int(raw))
    await state.set_state(PmMailing.hypothesis)
    await message.answer(HYPOTHESIS_ASK, reply_markup=_hypothesis_kb(pool))


@router.callback_query(PmMailing.hypothesis, ChoiceCB.filter(F.scope == "mail_hyp"))
async def mail_hypothesis(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext):
    """An open hypothesis, or none: the run's sends are counted under it."""
    number = None if callback_data.value == NO_HYPOTHESIS_VALUE else int(callback_data.value)
    await callback.answer()
    await state.update_data(hypothesis=number, hypothesis_text=None)
    await state.set_state(PmMailing.message)
    await callback.message.edit_text(SEND_MESSAGE_PROMPT, reply_markup=cancel_kb(PmMailing.message))


@router.message(PmMailing.hypothesis)
async def mail_new_hypothesis(message: Message, state: FSMContext):
    """A new hypothesis typed: made when the run starts (a mailing left before it makes none)."""
    text = await require_text(message)
    if text is None:
        return
    await state.update_data(hypothesis=None, hypothesis_text=text)
    await state.set_state(PmMailing.message)
    await message.answer(f"🧪 Новая гипотеза: «{text}».\n\n{SEND_MESSAGE_PROMPT}",
                         reply_markup=cancel_kb(PmMailing.message))


def _segment(data: dict) -> str:
    """Whom the run was for, as the report names it."""
    if data.get("recipients"):
        return f"список ({len(data['recipients'])})"
    return "📚 Вся база" if data["path"] == project_base.WHOLE else os.path.basename(data["path"])


def _start_run(pool: WorkerPool, data: dict, content, planned: int) -> int:
    """The run in the project's outreach (its new hypothesis made first): its number. A hypothesis picked but deleted
    since (🧪 Гипотезы): the run is under none, or its sends would be in no part of the report; closed since: under
    none too, its verdict was drawn without them."""
    home = pool.storage.home
    number = data.get("hypothesis")
    hypothesis = outreach.find_hypothesis(home, number) if number is not None else None
    if hypothesis is None or hypothesis.get("closed"):
        number = None
    if data.get("hypothesis_text"):
        number = outreach.add_hypothesis(home, data["hypothesis_text"])
    return outreach.add_run(home, number, _segment(data), content.text or "", len(content.media or ()), planned)


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
    elif data["path"] == project_base.WHOLE:  # 📚: the project's files glued, each person once
        try:
            recipients = await asyncio.to_thread(project_base.merged, data["project"],
                                                 project_base.worker_ids(pool.storage))
        except Exception as err:
            await end(f"⚠️ База не прочитана: {err}")
            return
    else:
        try:
            # off the event loop: a .parquet recipients DB can be large and load_recipients
            # is blocking (pq.read_table), which would otherwise stall the bot's polling loop
            recipients = await asyncio.to_thread(instance.load_recipients, data["path"])
        except Exception as err:
            await end(f"⚠️ Файл не прочитан: {err}")
            return

    def by_stats(rows):  # the gone ones out before the limit: they take no place in it; then the mailed, if asked
        instance.load_stats()
        alive = instance.drop_gone(rows)
        return alive, instance.filter_unsent(alive) if data.get("skip") else alive

    before = len(recipients)
    try:  # off the event loop, as the base: the stats grow with every recipient ever mailed
        alive, recipients = await asyncio.to_thread(by_stats, recipients)
    except Exception as err:
        await end(f"⚠️ Статистика не прочитана: {err}")
        return
    if gone := before - len(alive):
        await message.answer(f"{GONE_SKIPPED}: {gone}")

    if data.get("limit"):
        recipients = recipients[: data["limit"]]

    if not recipients:  # none at all, or none left once the gone, the mailed and the limit are out
        await end("Список получателей пуст." if not before else
                  "Некому писать: все уже получали рассылку или недоступны.")
        return

    content = await build_content(message, album)
    if content is None or manager.active:  # the run's data is read again with the next message: the step comes back
        await state.set_state(PmMailing.message)
        await state.update_data(**data)
        if content is not None:  # another job took the slot while the message was being read
            content.cleanup()
            await busy_step(message, manager, BASE)
        return

    async def job(f, report):
        """The run in the analytics once it starts (one that doesn't — no workers — leaves nothing there): its
        failure is said, never the mailing's."""
        try:
            run_id = await asyncio.to_thread(_start_run, pool, data, content, len(recipients))
        except Exception as err:
            run_id = None
            await report(f"⚠️ Аналитика этой рассылки не записывается: {err}")
        await f.run(recipients, content, report, run_id)

    await manager.run(
        message.bot, message.chat.id, pool, instance, bot_function, job,
        f"Рассылка в ЛС ({len(recipients)})…", cleanup=content.cleanup,
    )
