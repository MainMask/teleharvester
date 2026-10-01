from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import FunctionCB
from bot.routers._common import ensure_workers, resolve
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
    await callback.message.answer(f"Путь к .parquet (пусто = {DEFAULT_DB}):")


@router.message(AddContacts.path)
async def run(message: Message, state: FSMContext, pool: WorkerPool, functions: dict, manager: JobManager, settings: Settings):
    await state.clear()
    path = message.text.strip() or DEFAULT_DB

    instance, bot_function = resolve(functions, "addcontacts")
    await manager.run(
        message.bot, message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(path, settings.delay, r),
        "Добавление в контакты…", "Готово ✅",
    )
