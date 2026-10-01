from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import FunctionCB
from bot.routers._common import ensure_workers, resolve
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from bot.states import ChangeBio, ChangeName, ChangeUsername, SetPassword

router = Router()


# --- change bio ---

@router.callback_query(FunctionCB.filter(F.key == "bio"))
async def bio_start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool):
    await callback.answer()
    if not await ensure_workers(callback, pool):
        return
    await state.set_state(ChangeBio.text)
    await callback.message.answer("Введите новый текст bio:")


@router.message(ChangeBio.text)
async def bio_run(message: Message, state: FSMContext, pool: WorkerPool, functions: dict, manager: JobManager):
    await state.clear()
    instance, bot_function = resolve(functions, "bio")
    await manager.run(
        message.bot, message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(message.text, r),
        "Смена bio…", "Готово ✅",
    )


# --- change name ---

@router.callback_query(FunctionCB.filter(F.key == "name"))
async def name_start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool):
    await callback.answer()
    if not await ensure_workers(callback, pool):
        return
    await state.set_state(ChangeName.manual)
    await callback.message.answer("Введите имя (напр. «Иван Петров»):")


@router.message(ChangeName.manual)
async def name_run(message: Message, state: FSMContext, pool: WorkerPool, functions: dict, manager: JobManager):
    await state.clear()
    parts = message.text.split(maxsplit=1)
    first, last = parts[0], (parts[1] if len(parts) == 2 else None)
    instance, bot_function = resolve(functions, "name")
    await manager.run(
        message.bot, message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(r, first_name=first, last_name=last),
        "Смена имени…", "Готово ✅",
    )


# --- change username ---

@router.callback_query(FunctionCB.filter(F.key == "username"))
async def username_start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool):
    await callback.answer()
    if not await ensure_workers(callback, pool):
        return
    await state.set_state(ChangeUsername.base)
    await callback.message.answer("База для username (к ней добавится случайный суффикс):")


@router.message(ChangeUsername.base)
async def username_run(message: Message, state: FSMContext, pool: WorkerPool, functions: dict, manager: JobManager):
    await state.clear()
    instance, bot_function = resolve(functions, "username")
    await manager.run(
        message.bot, message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(r, base=message.text.strip()),
        "Смена username…", "Готово ✅",
    )


# --- change profile photo (no input) ---

@router.callback_query(FunctionCB.filter(F.key == "photo"))
async def photo_run(callback: CallbackQuery, pool: WorkerPool, functions: dict, manager: JobManager):
    await callback.answer()
    if not await ensure_workers(callback, pool):
        return
    instance, bot_function = resolve(functions, "photo")
    await manager.run(
        callback.bot, callback.message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(r),
        "Смена фото (из assets/photos/)…", "Готово ✅",
    )


# --- 2fa ---

@router.callback_query(FunctionCB.filter(F.key == "2fa"))
async def twofa_start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool):
    await callback.answer()
    if not await ensure_workers(callback, pool):
        return
    await state.set_state(SetPassword.password)
    await callback.message.answer("Введите новый пароль 2FA:")


@router.message(SetPassword.password)
async def twofa_run(message: Message, state: FSMContext, pool: WorkerPool, functions: dict, manager: JobManager):
    await state.clear()
    instance, bot_function = resolve(functions, "2fa")
    await manager.run(
        message.bot, message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(message.text, r),
        "Установка 2FA…", "Готово ✅",
    )
