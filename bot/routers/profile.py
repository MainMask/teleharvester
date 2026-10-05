import os
import uuid

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import ChoiceCB, FunctionCB
from bot.keyboards.common import choice_kb
from bot.routers._common import ensure_workers, read_text_document, require_text, resolve
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from bot.states import ChangeBio, ChangeName, ChangePhoto, ChangeUsername, SetPassword

router = Router()

PHOTO_TMP_DIR = os.path.join("tmp", "photo")  # project-local, like tmp/broadcast
NAMES_FILE = os.path.join("assets", "names.txt")          # the CLI's lists too
USERNAMES_FILE = os.path.join("assets", "usernames.txt")
MAX_LIST_FILE_SIZE = 1024 * 1024


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _file_lines(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8") as fileobj:
            return _lines(fileobj.read())
    except OSError:
        return []


def _file_kb(scope: str, path: str):
    """A button for the list in assets/, if there is one."""
    count = len(_file_lines(path))
    return choice_kb(scope, [(f"📄 Из {path} ({count})", "file")]) if count else None


async def _sent_lines(message: Message) -> list[str] | None:
    """The lines of a .txt the user sent; None (after a reply) if it's no usable list."""
    text = await read_text_document(message, MAX_LIST_FILE_SIZE)
    if text is None:
        return None
    lines = _lines(text)
    if not lines:
        await message.answer("В файле нет ни одной строки. Пришлите список заново.")
        return None
    return lines


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
    if not message.text:
        await message.answer("Ожидается текст. Попробуйте ещё раз.")
        return
    bio = message.text
    await state.clear()
    instance, bot_function = resolve(functions, "bio")
    await manager.run(
        message.bot, message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(bio, r),
        "Смена bio…", "Готово ✅",
    )


# --- change name ---

@router.callback_query(FunctionCB.filter(F.key == "name"))
async def name_start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool):
    await callback.answer()
    if not await ensure_workers(callback, pool):
        return
    await state.set_state(ChangeName.manual)
    await callback.message.answer(
        "Введите имя для всех аккаунтов (напр. «Иван Петров») — или пришлите .txt со списком "
        "имён, по одному на строку: каждый аккаунт получит случайное.",
        reply_markup=_file_kb("name_file", NAMES_FILE),
    )


async def _run_names(message: Message, pool: WorkerPool, functions: dict, manager: JobManager, **kwargs):
    instance, bot_function = resolve(functions, "name")
    await manager.run(
        message.bot, message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(r, **kwargs),
        "Смена имени…", "Готово ✅",
    )


@router.callback_query(ChangeName.manual, ChoiceCB.filter(F.scope == "name_file"))
async def name_from_file(callback: CallbackQuery, state: FSMContext, pool: WorkerPool, functions: dict,
                         manager: JobManager):
    await callback.answer()
    names = _file_lines(NAMES_FILE)
    if not names:
        return
    await state.clear()
    await _run_names(callback.message, pool, functions, manager, names=names)


@router.message(ChangeName.manual, F.document)
async def name_list(message: Message, state: FSMContext, pool: WorkerPool, functions: dict, manager: JobManager):
    names = await _sent_lines(message)
    if names is None:
        return
    await state.clear()
    await _run_names(message, pool, functions, manager, names=names)


@router.message(ChangeName.manual)
async def name_run(message: Message, state: FSMContext, pool: WorkerPool, functions: dict, manager: JobManager):
    name = await require_text(message)
    if name is None:
        return
    await state.clear()
    parts = name.split(maxsplit=1)
    first, last = parts[0], (parts[1] if len(parts) == 2 else None)
    await _run_names(message, pool, functions, manager, first_name=first, last_name=last)


# --- change username ---

@router.callback_query(FunctionCB.filter(F.key == "username"))
async def username_start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool):
    await callback.answer()
    if not await ensure_workers(callback, pool):
        return
    await state.set_state(ChangeUsername.base)
    await callback.message.answer(
        "База для username (если занят — добавится номер: base1, base2, …) — или пришлите .txt "
        "со списком username, по одному на строку: аккаунты получат их по порядку.",
        reply_markup=_file_kb("username_file", USERNAMES_FILE),
    )


async def _run_usernames(message: Message, pool: WorkerPool, functions: dict, manager: JobManager, **kwargs):
    instance, bot_function = resolve(functions, "username")
    await manager.run(
        message.bot, message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(r, **kwargs),
        "Смена username…", "Готово ✅",
    )


@router.callback_query(ChangeUsername.base, ChoiceCB.filter(F.scope == "username_file"))
async def username_from_file(callback: CallbackQuery, state: FSMContext, pool: WorkerPool, functions: dict,
                             manager: JobManager):
    await callback.answer()
    usernames = _file_lines(USERNAMES_FILE)
    if not usernames:
        return
    await state.clear()
    await _run_usernames(callback.message, pool, functions, manager, usernames=[u.lstrip("@") for u in usernames])


@router.message(ChangeUsername.base, F.document)
async def username_list(message: Message, state: FSMContext, pool: WorkerPool, functions: dict,
                        manager: JobManager):
    usernames = await _sent_lines(message)
    if usernames is None:
        return
    await state.clear()
    await _run_usernames(message, pool, functions, manager, usernames=[u.lstrip("@") for u in usernames])


@router.message(ChangeUsername.base)
async def username_run(message: Message, state: FSMContext, pool: WorkerPool, functions: dict, manager: JobManager):
    base = await require_text(message)
    if base is None:
        return
    await state.clear()
    await _run_usernames(message, pool, functions, manager, base=base)


# --- change profile photo ---

@router.callback_query(FunctionCB.filter(F.key == "photo"))
async def photo_start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool):
    await callback.answer()
    if not await ensure_workers(callback, pool):
        return
    await state.set_state(ChangePhoto.photo)
    await callback.message.answer(
        "Пришлите картинку (фото или файлом) — она станет аватаром всех аккаунтов:",
        reply_markup=choice_kb("photo_src", [("📁 Из папки assets/photos", "folder")]),
    )


@router.callback_query(ChangePhoto.photo, ChoiceCB.filter(F.scope == "photo_src"))
async def photo_folder(callback: CallbackQuery, state: FSMContext, pool: WorkerPool, functions: dict, manager: JobManager):
    await callback.answer()
    await state.clear()
    instance, bot_function = resolve(functions, "photo")
    await manager.run(
        callback.bot, callback.message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(r),
        "Смена фото (из assets/photos/)…", "Готово ✅",
    )


@router.message(ChangePhoto.photo)
async def photo_upload(message: Message, state: FSMContext, pool: WorkerPool, functions: dict, manager: JobManager):
    if message.photo:
        source = message.photo[-1]
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        source = message.document
    else:
        await message.answer("Ожидается картинка. Пришлите фото или файл-изображение.")
        return
    await state.clear()

    os.makedirs(PHOTO_TMP_DIR, exist_ok=True)
    path = os.path.join(PHOTO_TMP_DIR, f"{uuid.uuid4().hex}.jpg")
    try:
        await message.bot.download(source, destination=path, timeout=300)
    except Exception as err:  # e.g. the Bot API's ~20 MB download cap
        await message.answer(f"⚠️ Не удалось скачать картинку: {err}")
        return

    instance, bot_function = resolve(functions, "photo")

    async def job(func, reporter):
        try:
            await func.run(reporter, photo_path=path)
        finally:
            os.remove(path)

    started = await manager.run(
        message.bot, message.chat.id, pool, instance, bot_function, job,
        "Смена фото…", "Готово ✅",
        cleanup=lambda: os.remove(path),
    )
    if not started:
        os.remove(path)


# --- clear personal channel (no input) ---

@router.callback_query(FunctionCB.filter(F.key == "clearchannel"))
async def clearchannel_run(callback: CallbackQuery, pool: WorkerPool, functions: dict, manager: JobManager):
    await callback.answer()
    if not await ensure_workers(callback, pool):
        return
    instance, bot_function = resolve(functions, "clearchannel")
    await manager.run(
        callback.bot, callback.message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(r),
        "Очистка канала в профиле…", "Готово ✅",
    )


# --- hide last seen (no input) ---

@router.callback_query(FunctionCB.filter(F.key == "lastseen"))
async def lastseen_run(callback: CallbackQuery, pool: WorkerPool, functions: dict, manager: JobManager):
    await callback.answer()
    if not await ensure_workers(callback, pool):
        return
    instance, bot_function = resolve(functions, "lastseen")
    await manager.run(
        callback.bot, callback.message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(r),
        "Скрытие последнего визита…", "Готово ✅",
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
    if not message.text:
        await message.answer("Ожидается текст. Попробуйте ещё раз.")
        return
    password = message.text
    try:
        await message.delete()  # don't leave the password in the chat history
    except Exception:
        pass
    await state.clear()
    instance, bot_function = resolve(functions, "2fa")
    await manager.run(
        message.bot, message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(password, r),
        "Установка 2FA…", "Готово ✅",
    )
