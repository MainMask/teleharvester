from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import ChoiceCB, FunctionCB
from bot.keyboards.common import cancel_kb, choice_kb, yes_no_kb
from bot.routers._common import STALE_STEP, left_flow, require_text, resolve, start_form
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from bot.states import Join
from functions.joiner import CAPTCHA_WAIT

router = Router()


@router.callback_query(FunctionCB.filter(F.key == "join"))
async def start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool):
    await start_form(
        callback, state, Join.mode,
        "🚪 <b>Вступить в чат</b> — куда вступят воркеры?\n\n"
        "🚪 <b>По ссылке</b> — в канал или группу, куда она ведёт (и приглашение t.me/+…).\n"
        "💬 <b>В комментарии</b> — в группу комментариев канала по его публичной ссылке: чтобы писать под "
        "постами.",
        pool, choice_kb("join_mode", [("🚪 По ссылке", "1"), ("💬 В комментарии", "2")]), parse_mode="HTML",
    )


@router.callback_query(ChoiceCB.filter(F.scope == "join_mode"))
async def pick_mode(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext):
    if await left_flow(callback, state, Join.mode):
        return
    await state.update_data(mode=callback_data.value)
    await callback.message.edit_text(
        f"Нажимать капчу после вступления? (каждый воркер ждёт её до {CAPTCHA_WAIT} с)",
        reply_markup=cancel_kb(Join.mode, yes_no_kb("join_captcha")),
    )
    await callback.answer()


@router.callback_query(ChoiceCB.filter(F.scope == "join_captcha"))
async def pick_captcha(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext):
    if await left_flow(callback, state, Join.mode):
        return
    await state.update_data(captcha=callback_data.value == "yes")
    await state.set_state(Join.link)
    await callback.message.edit_text("🚪 Вступить в чат — пришлите ссылку на чат или канал:",
                                     reply_markup=cancel_kb(Join.link))
    await callback.answer()


@router.message(Join.link)
async def got_link(
    message: Message,
    state: FSMContext,
    pool: WorkerPool,
    functions: dict,
    manager: JobManager,
):
    link = await require_text(message)
    if link is None:
        return

    data = await state.get_data()
    await state.clear()

    if "mode" not in data:  # stale flow after a state clear
        await message.answer(STALE_STEP)
        return

    instance, bot_function = resolve(functions, "join")
    await manager.run(
        message.bot, message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(data["mode"], link, None, r, captcha=data.get("captcha", False)),
        "Вступление…",
    )
