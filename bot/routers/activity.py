from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import ChoiceCB, FunctionCB
from bot.routers._common import ensure_workers, require_text, resolve
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from bot.states import PollVote, Reactions

router = Router()

REACTIONS = ['👍', '❤️', '🔥', '🥰', '👏', '😁', '🎉', '🤩', '👎', '🤯', '😱', '🤬', '😢', '🤮', '💩', '🙏']


def _reactions_kb():
    builder = InlineKeyboardBuilder()
    for emoji in REACTIONS:
        builder.button(text=emoji, callback_data=ChoiceCB(scope="reaction", value=emoji))
    builder.button(text="🎲 Random", callback_data=ChoiceCB(scope="reaction", value="random"))
    builder.adjust(4)
    return builder.as_markup()


# --- reactions ---

@router.callback_query(FunctionCB.filter(F.key == "reactions"))
async def reactions_start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool):
    await callback.answer()
    if not await ensure_workers(callback, pool):
        return
    await state.set_state(Reactions.link)
    await callback.message.answer("Ссылка на сообщение/пост:")


@router.message(Reactions.link)
async def reactions_link(message: Message, state: FSMContext):
    link = await require_text(message)
    if link is None:
        return
    await state.update_data(link=link)
    await message.answer("Выберите реакцию:", reply_markup=_reactions_kb())


@router.callback_query(ChoiceCB.filter(F.scope == "reaction"))
async def reactions_run(
    callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext,
    pool: WorkerPool, functions: dict, manager: JobManager,
):
    data = await state.get_data()
    await state.clear()
    await callback.answer()

    if "link" not in data:  # a stale reaction button after the state was cleared
        await callback.message.answer("Флоу устарел, начните заново.")
        return

    reaction = "" if callback_data.value == "random" else callback_data.value
    instance, bot_function = resolve(functions, "reactions")
    await manager.run(
        callback.bot, callback.message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(data["link"], reaction, r),
        "Реакции…", "Готово ✅",
    )


# --- poll vote ---

@router.callback_query(FunctionCB.filter(F.key == "poll"))
async def poll_start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool):
    await callback.answer()
    if not await ensure_workers(callback, pool):
        return
    await state.set_state(PollVote.link)
    await callback.message.answer("Ссылка на сообщение/пост с опросом:")


@router.message(PollVote.link)
async def poll_link(message: Message, state: FSMContext):
    link = await require_text(message)
    if link is None:
        return
    await state.update_data(link=link)
    await state.set_state(PollVote.option)
    await message.answer("Номер варианта (напр. 1, 2):")


@router.message(PollVote.option)
async def poll_run(message: Message, state: FSMContext, pool: WorkerPool, functions: dict, manager: JobManager):
    raw = (message.text or "").strip()
    if not raw.isdecimal() or int(raw) < 1:
        await message.answer("Введите число ≥ 1:")
        return

    data = await state.get_data()
    await state.clear()
    option = int(raw) - 1

    instance, bot_function = resolve(functions, "poll")
    await manager.run(
        message.bot, message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(data["link"], option, r),
        "Голосование…", "Готово ✅",
    )
