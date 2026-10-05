from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import ChoiceCB, FunctionCB
from bot.keyboards.common import choice_kb
from bot.routers._common import ensure_workers, resolve
from modules import scraped_files
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from bot.states import AddContacts
from modules.settings import Settings

router = Router()

DEFAULT_DB = "assets/contacts.parquet"


@router.callback_query(FunctionCB.filter(F.key == "addcontacts"))
async def start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool):
    await callback.answer()
    if not await ensure_workers(callback, pool):
        return
    await state.set_state(AddContacts.path)
    bases = scraped_files.participant_bases()
    await state.update_data(bases=[path for path, _ in bases])  # buttons carry an index into this
    choose = "Выберите базу из скрапа ниже или укажите путь" if bases else "Путь"
    await callback.message.answer(
        f"{choose} к .parquet («-» = {DEFAULT_DB}):",
        reply_markup=choice_kb("contacts_base", [(label, str(i)) for i, (_, label) in enumerate(bases)])
        if bases else None,
    )


async def _launch(message: Message, path: str, pool: WorkerPool, functions: dict, manager: JobManager,
                  settings: Settings):
    instance, bot_function = resolve(functions, "addcontacts")
    await manager.run(
        message.bot, message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(path, settings.delay, r),
        "Добавление в контакты…", "Готово ✅",
    )


@router.callback_query(AddContacts.path, ChoiceCB.filter(F.scope == "contacts_base"))
async def run_base(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext, pool: WorkerPool,
                   functions: dict, manager: JobManager, settings: Settings):
    await callback.answer()
    bases = (await state.get_data()).get("bases", [])
    index = int(callback_data.value)
    if not 0 <= index < len(bases):
        return
    await state.clear()
    await _launch(callback.message, bases[index], pool, functions, manager, settings)


@router.message(AddContacts.path)
async def run(message: Message, state: FSMContext, pool: WorkerPool, functions: dict, manager: JobManager, settings: Settings):
    await state.clear()
    raw = (message.text or "").strip()
    path = DEFAULT_DB if raw in ("", "-") else raw  # Telegram can't send an empty message
    await _launch(message, path, pool, functions, manager, settings)
