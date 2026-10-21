from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import ChoiceCB
from bot.keyboards.common import cancel_kb, choice_kb
from bot.routers._common import pick_from, require_text, resolve, stale_tap
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from bot.states import AddContacts

router = Router()

DEFAULT_DB = "assets/contacts.parquet"


async def ask_base(message: Message, state: FSMContext, bases: list[tuple[str, str]]):
    """The base to add to contacts, from a project's 📨 База и рассылка (bot/routers/projects/card.py): by button
    (`bases`: the project's) or a path."""
    await state.clear()  # an abandoned flow's data must not reach this one
    await state.set_state(AddContacts.path)
    await state.update_data(bases=[path for path, _ in bases])  # buttons carry an index into this
    choose = "выберите базу из скрапа ниже или пришлите" if bases else "пришлите"
    await message.answer(
        f"📇 В контакты — {choose} путь к .parquet на сервере:",
        reply_markup=cancel_kb(AddContacts.path, choice_kb(
            "contacts_base", [*((f"📄 {label}", str(i)) for i, (_, label) in enumerate(bases)),
                              (f"📄 {DEFAULT_DB}", "default")])),
    )


async def _launch(message: Message, path: str, pool: WorkerPool, functions: dict, manager: JobManager):
    instance, bot_function = resolve(functions, "addcontacts")
    await manager.run(
        message.bot, message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(path, None, r),
        "Добавление в контакты…",
    )


@router.callback_query(AddContacts.path, ChoiceCB.filter(F.scope == "contacts_base"))
async def run_base(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext, pool: WorkerPool,
                   functions: dict, manager: JobManager):
    path = DEFAULT_DB if callback_data.value == "default" else \
        pick_from((await state.get_data()).get("bases", []), callback_data.value)
    if path is None:  # a button of an older list of the bases
        await stale_tap(callback)
        return
    await callback.answer()
    await state.clear()
    await _launch(callback.message, path, pool, functions, manager)


@router.message(AddContacts.path)
async def run(message: Message, state: FSMContext, pool: WorkerPool, functions: dict, manager: JobManager):
    raw = await require_text(message)  # a file / photo must not fall back to the default base
    if raw is None:
        return
    await state.clear()
    path = DEFAULT_DB if raw == "-" else raw  # «-»: as before its button
    await _launch(message, path, pool, functions, manager)
