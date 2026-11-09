import asyncio

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import ChoiceCB
from bot.keyboards.common import cancel_kb, choice_kb
from bot.routers._common import back_answer, busy_step, busy_tap, pick_from, require_text, resolve, stale_tap
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from bot.states import AddContacts
from modules import project_base
from modules.registry import BASE, title

router = Router()

DEFAULT_DB = "assets/contacts.parquet"


async def ask_base(message: Message, state: FSMContext, project: str, whole: int, bases: list[tuple[str, str]]):
    """The base to add to contacts, from a project's 🎯 База и рассылка (bot/routers/projects/card.py): 📚 its whole
    base (`whole` people with an id, each once: project_base) or one of its files (`bases`) by button, or a path."""
    await state.clear()  # an abandoned flow's data must not reach this one
    await state.set_state(AddContacts.path)
    await state.update_data(bases=[path for path, _ in bases], project=project)  # buttons carry an index into this
    await message.edit_text(  # in 🎯 База и рассылка's place, as a choice by button
        f"{title('addcontacts')} — выберите базу ниже или пришлите путь к .parquet на сервере:",
        reply_markup=cancel_kb(AddContacts.path, choice_kb(
            "contacts_base", [*([(f"📚 Вся база · {whole}", project_base.WHOLE)] if bases else []),
                              *((f"📄 {label}", str(i)) for i, (_, label) in enumerate(bases)),
                              (f"📄 {DEFAULT_DB}", "default")])),
    )


async def _launch(message: Message, source, pool: WorkerPool, functions: dict, manager: JobManager):
    """`source`: a base's path, or (📚) the project's: its people with an id, read when the run starts."""
    instance, bot_function = resolve(functions, "addcontacts")
    await manager.run(
        message.bot, message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(source, r),
        "Добавление в контакты…",
    )


@router.callback_query(AddContacts.path, ChoiceCB.filter(F.scope == "contacts_base"))
async def run_base(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext, pool: WorkerPool,
                   functions: dict, manager: JobManager):
    data = await state.get_data()
    whole = callback_data.value == project_base.WHOLE
    if whole:
        path = data.get("project")  # 📚: the project's base; None for a button of a flow gone
    elif callback_data.value == "default":
        path = DEFAULT_DB
    else:
        path = pick_from(data.get("bases", []), callback_data.value)
    if path is None:  # a button of an older list of the bases
        await stale_tap(callback)
        return
    if await busy_tap(callback, manager):
        return
    await callback.answer()
    await state.clear()
    await callback.message.edit_reply_markup(reply_markup=None)  # chosen once: one run
    if whole:  # 📚: its people with an id, each once; off the event loop, as a base's file is read
        try:
            found = await asyncio.to_thread(project_base.merged, path, project_base.worker_ids(pool.storage))
        except Exception as err:  # as the mailing's 📚
            await back_answer(callback.message, f"⚠️ База не прочитана: {err}", BASE, manager.project_id)
            return
        path = [person for person in found if isinstance(person, dict)]
    await _launch(callback.message, path, pool, functions, manager)


@router.message(AddContacts.path)
async def run(message: Message, state: FSMContext, pool: WorkerPool, functions: dict, manager: JobManager):
    raw = await require_text(message)  # a file / photo must not fall back to the default base
    if raw is None:
        return
    if manager.active:
        await busy_step(message, manager, BASE, "пришлите путь ещё раз")
        return
    await state.clear()
    path = DEFAULT_DB if raw == "-" else raw  # «-»: as before its button
    await _launch(message, path, pool, functions, manager)
