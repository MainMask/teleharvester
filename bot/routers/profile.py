import os
import uuid

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import ChoiceCB, FunctionCB
from bot.keyboards.common import choice_kb
from bot.routers._common import ensure_workers, read_text_document, require_text, resolve
from bot.services.registry import BOT_FUNCTIONS_BY_KEY
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from bot.states import ChangeBio, ChangeName, ChangePhoto, ChangeUsername, PickWorkers, SetPassword
from modules import profile_done, restricted_workers
from modules.scraper_creds import scrape_accounts

router = Router()

PHOTO_TMP_DIR = os.path.join("tmp", "photo")  # project-local, like tmp/broadcast
NAMES_FILE = os.path.join("assets", "names.txt")          # the CLI's lists too
USERNAMES_FILE = os.path.join("assets", "usernames.txt")
BIOS_FILE = os.path.join("assets", "bios.txt")
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


# --- worker picker: a function's first step, so workers done earlier can be left out ---

PAGE_SIZE = 20  # workers per page of the picker


def _workers_text(picked: list[int], total: int, new: list[int]) -> str:
    text = (f"На каких воркерах? Отмечено: {len(picked)} из {total}\n"
            f"✓ — уже менялось этой функцией; отмечены новые ({len(new)})")
    return text if new else text + "\nНовых нет — отметьте вручную."


def _workers_kb(labels: list[str], picked: list[int], page: int):
    pages = (len(labels) + PAGE_SIZE - 1) // PAGE_SIZE
    first = page * PAGE_SIZE
    builder = InlineKeyboardBuilder()
    for index in range(first, min(first + PAGE_SIZE, len(labels))):
        mark = "✅" if index in picked else "⬜"
        builder.button(text=f"{mark} {labels[index]}", callback_data=ChoiceCB(scope="wpick", value=str(index)))
    nav = []
    if pages > 1:
        nav = [("◀", "prev"), (f"стр {page + 1}/{pages}", "page"), ("▶", "next")]
    for text, value in [*nav, ("☑️ Все", "all"), ("⬜ Никого", "none"), ("🆕 Только новые", "new"), ("Далее ▶", "go")]:
        builder.button(text=text, callback_data=ChoiceCB(scope="wpick", value=value))
    builder.adjust(*[1] * (min(first + PAGE_SIZE, len(labels)) - first), *([3] if nav else []), 3, 1)
    return builder.as_markup()


async def _ask_workers(callback: CallbackQuery, state: FSMContext, key: str, deps: dict):
    """Ask which workers the function `key` runs on, the ones it has not changed yet ticked
    (see modules.profile_done), then go on to its _NEXT step."""
    pool = deps["pool"]
    accounts = scrape_accounts(pool.storage)
    keys = [profile_done.worker_key(pool.storage, account.path) for account in accounts]
    profile_done.seed(keys)
    ledger, classname = profile_done.load(), BOT_FUNCTIONS_BY_KEY[key].classname
    forever, status = set(restricted_workers.load()), restricted_workers.load_status()
    scraping = pool.scraping.path if pool.scraping is not None else None

    labels = []
    for account, worker in zip(accounts, keys):
        notes = profile_done.notes(worker, account.path, classname, ledger, forever, status)
        if account.path == scraping:
            notes.append("⏳ скрап")
        labels.append(" ".join([account.label, *notes]))
    new = [i for i, worker in enumerate(keys) if classname not in ledger["done"].get(worker, {})]

    await state.set_state(PickWorkers.choose)
    await state.update_data(workers=None, pick_key=key, pick_paths=[account.path for account in accounts],
                            pick_labels=labels, pick_new=new, picked=new, pick_page=0)
    await callback.message.answer(_workers_text(new, len(labels), new), reply_markup=_workers_kb(labels, new, 0))


@router.callback_query(PickWorkers.choose, ChoiceCB.filter(F.scope == "wpick"))
async def workers_pick(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext, pool: WorkerPool,
                       functions: dict, manager: JobManager):
    data = await state.get_data()
    paths, labels, new = data["pick_paths"], data["pick_labels"], data["pick_new"]
    picked, page = data["picked"], data["pick_page"]
    value = callback_data.value

    if value == "go":
        if not picked:
            await callback.answer("Отметьте хотя бы одного воркера", show_alert=True)
            return
        await callback.answer()
        await state.update_data(workers=None if len(picked) == len(paths) else [paths[i] for i in picked])
        await callback.message.edit_text(f"Воркеры: {len(picked)} из {len(paths)}")
        await _NEXT[data["pick_key"]](callback.message, state,
                                      dict(pool=pool, functions=functions, manager=manager))
        return

    await callback.answer()
    pages = (len(paths) + PAGE_SIZE - 1) // PAGE_SIZE
    new_picked, new_page = picked, page
    if value == "all":
        new_picked = list(range(len(paths)))
    elif value == "none":
        new_picked = []
    elif value == "new":
        new_picked = new
    elif value == "prev":
        new_page = (page - 1) % pages
    elif value == "next":
        new_page = (page + 1) % pages
    elif value.isdigit() and int(value) < len(paths):
        new_picked = sorted(set(picked) ^ {int(value)})
    if (new_picked, new_page) == (picked, page):  # an unchanged message can't be edited ("page" too)
        return
    await state.update_data(picked=new_picked, pick_page=new_page)
    await callback.message.edit_text(_workers_text(new_picked, len(labels), new),
                                     reply_markup=_workers_kb(labels, new_picked, new_page))


@router.callback_query(ChoiceCB.filter(F.scope == "wpick"))
async def workers_pick_stale(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer("Флоу устарел, начните заново.")


async def _picked(state: FSMContext) -> list[str] | None:
    """The session paths of the workers picked for the job; None: all of them."""
    return (await state.get_data()).get("workers")


# --- change bio ---

@router.callback_query(FunctionCB.filter(F.key == "bio"))
async def bio_start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool, functions: dict,
                    manager: JobManager):
    await callback.answer()
    if not await ensure_workers(callback, pool):
        return
    await _ask_workers(callback, state, "bio", dict(pool=pool, functions=functions, manager=manager))


async def _bio_prompt(message: Message, state: FSMContext, deps: dict):
    await state.set_state(ChangeBio.text)
    await message.answer(
        "Введите новый текст bio для выбранных аккаунтов — или пришлите .txt со списком вариантов, "
        "по одному на строку: каждый аккаунт получит случайный.",
        reply_markup=_file_kb("bio_file", BIOS_FILE),
    )


async def _run_bios(message: Message, pool: WorkerPool, functions: dict, manager: JobManager, only, **kwargs):
    instance, bot_function = resolve(functions, "bio")
    await manager.run(
        message.bot, message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(r, **kwargs),
        "Смена bio…", "Готово ✅",
        only=only,
    )


@router.callback_query(ChangeBio.text, ChoiceCB.filter(F.scope == "bio_file"))
async def bio_from_file(callback: CallbackQuery, state: FSMContext, pool: WorkerPool, functions: dict,
                        manager: JobManager):
    await callback.answer()
    bios = _file_lines(BIOS_FILE)
    if not bios:
        return
    only = await _picked(state)
    await state.clear()
    await _run_bios(callback.message, pool, functions, manager, only, bios=bios)


@router.message(ChangeBio.text, F.document)
async def bio_list(message: Message, state: FSMContext, pool: WorkerPool, functions: dict, manager: JobManager):
    bios = await _sent_lines(message)
    if bios is None:
        return
    only = await _picked(state)
    await state.clear()
    await _run_bios(message, pool, functions, manager, only, bios=bios)


@router.message(ChangeBio.text)
async def bio_run(message: Message, state: FSMContext, pool: WorkerPool, functions: dict, manager: JobManager):
    if not message.text:
        await message.answer("Ожидается текст. Попробуйте ещё раз.")
        return
    bio = message.text
    only = await _picked(state)
    await state.clear()
    await _run_bios(message, pool, functions, manager, only, bio=bio)


# --- change name ---

@router.callback_query(FunctionCB.filter(F.key == "name"))
async def name_start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool, functions: dict,
                     manager: JobManager):
    await callback.answer()
    if not await ensure_workers(callback, pool):
        return
    await _ask_workers(callback, state, "name", dict(pool=pool, functions=functions, manager=manager))


async def _name_prompt(message: Message, state: FSMContext, deps: dict):
    await state.set_state(ChangeName.manual)
    await message.answer(
        "Введите имя для выбранных аккаунтов (напр. «Иван Петров») — или пришлите .txt со списком "
        "имён, по одному на строку: каждый аккаунт получит случайное.",
        reply_markup=_file_kb("name_file", NAMES_FILE),
    )


async def _run_names(message: Message, pool: WorkerPool, functions: dict, manager: JobManager, only, **kwargs):
    instance, bot_function = resolve(functions, "name")
    await manager.run(
        message.bot, message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(r, **kwargs),
        "Смена имени…", "Готово ✅",
        only=only,
    )


@router.callback_query(ChangeName.manual, ChoiceCB.filter(F.scope == "name_file"))
async def name_from_file(callback: CallbackQuery, state: FSMContext, pool: WorkerPool, functions: dict,
                         manager: JobManager):
    await callback.answer()
    names = _file_lines(NAMES_FILE)
    if not names:
        return
    only = await _picked(state)
    await state.clear()
    await _run_names(callback.message, pool, functions, manager, only, names=names)


@router.message(ChangeName.manual, F.document)
async def name_list(message: Message, state: FSMContext, pool: WorkerPool, functions: dict, manager: JobManager):
    names = await _sent_lines(message)
    if names is None:
        return
    only = await _picked(state)
    await state.clear()
    await _run_names(message, pool, functions, manager, only, names=names)


@router.message(ChangeName.manual)
async def name_run(message: Message, state: FSMContext, pool: WorkerPool, functions: dict, manager: JobManager):
    name = await require_text(message)
    if name is None:
        return
    only = await _picked(state)
    await state.clear()
    parts = name.split(maxsplit=1)
    first, last = parts[0], (parts[1] if len(parts) == 2 else None)
    await _run_names(message, pool, functions, manager, only, first_name=first, last_name=last)


# --- change username ---

@router.callback_query(FunctionCB.filter(F.key == "username"))
async def username_start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool, functions: dict,
                         manager: JobManager):
    await callback.answer()
    if not await ensure_workers(callback, pool):
        return
    await _ask_workers(callback, state, "username", dict(pool=pool, functions=functions, manager=manager))


async def _username_prompt(message: Message, state: FSMContext, deps: dict):
    await state.set_state(ChangeUsername.base)
    await message.answer(
        "База для username (если занята — добавится случайное число: base24, base3071, …) — или пришлите .txt "
        "со списком username, по одному на строку: аккаунты получат их по порядку.",
        reply_markup=_file_kb("username_file", USERNAMES_FILE),
    )


async def _run_usernames(message: Message, pool: WorkerPool, functions: dict, manager: JobManager, only,
                         **kwargs):
    instance, bot_function = resolve(functions, "username")
    await manager.run(
        message.bot, message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(r, **kwargs),
        "Смена username…", "Готово ✅",
        only=only,
    )


@router.callback_query(ChangeUsername.base, ChoiceCB.filter(F.scope == "username_file"))
async def username_from_file(callback: CallbackQuery, state: FSMContext, pool: WorkerPool, functions: dict,
                             manager: JobManager):
    await callback.answer()
    usernames = _file_lines(USERNAMES_FILE)
    if not usernames:
        return
    only = await _picked(state)
    await state.clear()
    await _run_usernames(callback.message, pool, functions, manager, only, usernames=[u.lstrip("@") for u in usernames])


@router.message(ChangeUsername.base, F.document)
async def username_list(message: Message, state: FSMContext, pool: WorkerPool, functions: dict,
                        manager: JobManager):
    usernames = await _sent_lines(message)
    if usernames is None:
        return
    only = await _picked(state)
    await state.clear()
    await _run_usernames(message, pool, functions, manager, only, usernames=[u.lstrip("@") for u in usernames])


@router.message(ChangeUsername.base)
async def username_run(message: Message, state: FSMContext, pool: WorkerPool, functions: dict, manager: JobManager):
    base = await require_text(message)
    if base is None:
        return
    only = await _picked(state)
    await state.clear()
    await _run_usernames(message, pool, functions, manager, only, base=base)


# --- change profile photo ---

@router.callback_query(FunctionCB.filter(F.key == "photo"))
async def photo_start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool, functions: dict,
                      manager: JobManager):
    await callback.answer()
    if not await ensure_workers(callback, pool):
        return
    await _ask_workers(callback, state, "photo", dict(pool=pool, functions=functions, manager=manager))


async def _photo_prompt(message: Message, state: FSMContext, deps: dict):
    await state.set_state(ChangePhoto.photo)
    await message.answer(
        "Пришлите картинку (фото или файлом) — она станет аватаром выбранных аккаунтов, прежние аватарки удалятся:",
        reply_markup=choice_kb("photo_src", [("📁 Из папки assets/photos", "folder")]),
    )


@router.callback_query(ChangePhoto.photo, ChoiceCB.filter(F.scope == "photo_src"))
async def photo_folder(callback: CallbackQuery, state: FSMContext, pool: WorkerPool, functions: dict, manager: JobManager):
    await callback.answer()
    only = await _picked(state)
    await state.clear()
    instance, bot_function = resolve(functions, "photo")
    await manager.run(
        callback.bot, callback.message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(r),
        "Смена фото (из assets/photos/)…", "Готово ✅",
        only=only,
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
    only = await _picked(state)
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
        only=only,
    )
    if not started:
        os.remove(path)


# --- clear personal channel (no input) ---

@router.callback_query(FunctionCB.filter(F.key == "clearchannel"))
async def clearchannel_start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool, functions: dict,
                             manager: JobManager):
    await callback.answer()
    if not await ensure_workers(callback, pool):
        return
    await _ask_workers(callback, state, "clearchannel", dict(pool=pool, functions=functions, manager=manager))


async def _clearchannel_run(message: Message, state: FSMContext, deps: dict):
    only = await _picked(state)
    await state.clear()
    instance, bot_function = resolve(deps["functions"], "clearchannel")
    await deps["manager"].run(
        message.bot, message.chat.id, deps["pool"], instance, bot_function,
        lambda f, r: f.run(r),
        "Очистка канала в профиле…", "Готово ✅",
        only=only,
    )


# --- hide last seen (no input) ---

@router.callback_query(FunctionCB.filter(F.key == "lastseen"))
async def lastseen_start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool, functions: dict,
                         manager: JobManager):
    await callback.answer()
    if not await ensure_workers(callback, pool):
        return
    await _ask_workers(callback, state, "lastseen", dict(pool=pool, functions=functions, manager=manager))


async def _lastseen_run(message: Message, state: FSMContext, deps: dict):
    only = await _picked(state)
    await state.clear()
    instance, bot_function = resolve(deps["functions"], "lastseen")
    await deps["manager"].run(
        message.bot, message.chat.id, deps["pool"], instance, bot_function,
        lambda f, r: f.run(r),
        "Скрытие последнего визита…", "Готово ✅",
        only=only,
    )


# --- 2fa ---

@router.callback_query(FunctionCB.filter(F.key == "2fa"))
async def twofa_start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool, functions: dict,
                      manager: JobManager):
    await callback.answer()
    if not await ensure_workers(callback, pool):
        return
    await _ask_workers(callback, state, "2fa", dict(pool=pool, functions=functions, manager=manager))


async def _twofa_prompt(message: Message, state: FSMContext, deps: dict):
    await state.set_state(SetPassword.password)
    await message.answer("Введите новый пароль 2FA:")


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
    only = await _picked(state)
    await state.clear()
    instance, bot_function = resolve(functions, "2fa")
    await manager.run(
        message.bot, message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(password, r),
        "Установка 2FA…", "Готово ✅",
        only=only,
    )


# --- terminate other sessions (no input) ---

@router.callback_query(FunctionCB.filter(F.key == "terminate"))
async def terminate_start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool, functions: dict,
                          manager: JobManager):
    await callback.answer()
    if not await ensure_workers(callback, pool):
        return
    await _ask_workers(callback, state, "terminate", dict(pool=pool, functions=functions, manager=manager))


async def _terminate_run(message: Message, state: FSMContext, deps: dict):
    only = await _picked(state)
    await state.clear()
    instance, bot_function = resolve(deps["functions"], "terminate")
    await deps["manager"].run(
        message.bot, message.chat.id, deps["pool"], instance, bot_function,
        lambda f, r: f.run(r),
        "Сброс чужих сессий…", "Готово ✅",
        only=only,
    )


# the step after the worker picker, by function key
_NEXT = {
    "bio": _bio_prompt,
    "name": _name_prompt,
    "username": _username_prompt,
    "photo": _photo_prompt,
    "clearchannel": _clearchannel_run,
    "lastseen": _lastseen_run,
    "2fa": _twofa_prompt,
    "terminate": _terminate_run,
}
