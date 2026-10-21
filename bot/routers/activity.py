import asyncio

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import ChoiceCB, FunctionCB
from bot.keyboards.common import cancel_kb, choice_kb
from bot.routers._common import STALE_BUTTON, require_post_link, resolve, start_form
from bot.services.delegation import WorkerPool, borrow
from bot.services.jobs import JobManager
from bot.states import PollVote, Reactions
from functions.reactions import ReactionsFunc
from scraper.scrape import CHAT_LOOKUP_TIMEOUT

router = Router()


def _reactions_kb():
    return choice_kb("reaction", [*((emoji, emoji) for emoji in ReactionsFunc.reactions),
                                  ("🎲 Случайная у каждого", "random")], widths=(4,))


# --- reactions ---

@router.callback_query(FunctionCB.filter(F.key == "reactions"))
async def reactions_start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool):
    await start_form(callback, state, Reactions.link, "❤️ Реакции — пришлите ссылку на сообщение или пост:", pool)


@router.message(Reactions.link)
async def reactions_link(message: Message, state: FSMContext):
    link = await require_post_link(message)
    if link is None:
        return
    await state.update_data(link=link)
    await message.answer("❤️ Какую реакцию ставят воркеры?", reply_markup=cancel_kb(Reactions.link, _reactions_kb()))


@router.callback_query(ChoiceCB.filter(F.scope == "reaction"))
async def reactions_run(
    callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext,
    pool: WorkerPool, functions: dict, manager: JobManager,
):
    data = await state.get_data()
    await callback.answer()
    # a stale reaction button: after the state was cleared, or in another flow that has a link of its own
    if await state.get_state() != Reactions.link.state or "link" not in data:
        await callback.message.answer(STALE_BUTTON)
        return
    await state.clear()
    await callback.message.edit_reply_markup(reply_markup=None)  # chosen once: one run

    reaction = "" if callback_data.value == "random" else callback_data.value
    instance, bot_function = resolve(functions, "reactions")
    await manager.run(
        callback.bot, callback.message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(data["link"], reaction, r),
        "Реакции…",
    )


# --- poll vote ---

@router.callback_query(FunctionCB.filter(F.key == "poll"))
async def poll_start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool):
    await start_form(callback, state, PollVote.link, "🗳 Голосовать — пришлите ссылку на сообщение или пост с опросом:",
                     pool)


POLL_ASK = "Пришлите номер варианта в опросе, например 1 или 2:"


async def _poll_answers(pool: WorkerPool, functions: dict, link: str) -> list[str] | str:
    """The poll's options, read on a free worker (its own client, see borrow); why not, when they can't be
    read now: then the option's number is typed."""
    client = None
    for worker in pool.workers:  # one account, one job: not one a job, the auto-reply or a scrape uses
        path = pool.storage.get_session_path(worker)
        if not (pool.busy(path) or pool.scraped(path)):
            client = worker
            break
    if client is None:
        return "все воркеры сейчас заняты"
    instance, _ = resolve(functions, "poll")
    try:
        return await borrow(pool, client, path, lambda: instance.answers(client, link), CHAT_LOOKUP_TIMEOUT)
    except asyncio.CancelledError:
        if asyncio.current_task().cancelling():  # the bot is stopping
            raise
        return "соединение прервано"  # a job's end released the worker mid-read
    except (Exception, SystemExit) as err:  # SystemExit: a logged-out session
        return str(err) or "Telegram не ответил вовремя"


@router.message(PollVote.link)
async def poll_link(message: Message, state: FSMContext, pool: WorkerPool, functions: dict):
    link = await require_post_link(message)
    if link is None:
        return
    await state.update_data(link=link)
    await state.set_state(PollVote.option)
    answers = await _poll_answers(pool, functions, link)
    if isinstance(answers, str):  # the number typed, as before
        await message.answer(f"ℹ️ Варианты не прочитаны: {answers}.\n{POLL_ASK}",
                             reply_markup=cancel_kb(PollVote.option))
        return
    options = [(f"{number}. {text[:40]}", str(number - 1)) for number, text in enumerate(answers, 1)]
    await message.answer("🗳 За какой вариант голосуют воркеры?",
                         reply_markup=cancel_kb(PollVote.option, choice_kb("poll_opt", options)))


@router.callback_query(PollVote.option, ChoiceCB.filter(F.scope == "poll_opt"))
async def poll_pick(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext, pool: WorkerPool,
                    functions: dict, manager: JobManager):
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)  # chosen once: one vote
    await _vote(callback.message, state, pool, functions, manager, int(callback_data.value))


@router.message(PollVote.option)
async def poll_run(message: Message, state: FSMContext, pool: WorkerPool, functions: dict, manager: JobManager):
    raw = (message.text or "").strip()
    if not raw.isdecimal() or int(raw) < 1:
        await message.answer("Нужен номер варианта — число от 1. Пришлите ещё раз:")
        return
    await _vote(message, state, pool, functions, manager, int(raw) - 1)


async def _vote(message: Message, state: FSMContext, pool: WorkerPool, functions: dict, manager: JobManager,
                option: int):
    data = await state.get_data()
    await state.clear()
    instance, bot_function = resolve(functions, "poll")
    await manager.run(
        message.bot, message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(data["link"], option, r),
        "Голосование…",
    )
