import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import ChoiceCB, FunctionCB
from bot.keyboards.common import cancel_kb, choice_kb, marks_kb
from bot.routers._common import (back_answer, delete_quietly, ensure_workers, marks_step, read_text_document,
                                 require_text, resolve)
from modules.registry import BOT_FUNCTIONS_BY_KEY, PROFILE
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from bot.states import ChangePhoto, PickWorkers, ProfileText, SetPassword
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
        await message.answer("В файле нет ни одной строки. Пришлите ещё раз:")
        return None
    return lines


# --- worker picker: a function's first step, so workers done earlier can be left out ---

def _workers_text(key: str, picked: list[int], total: int, new: list[int]) -> str:
    text = (f"{BOT_FUNCTIONS_BY_KEY[key].title} — на каких воркерах? Отмечено: {len(picked)} из {total}\n"
            f"✓ — эта функция уже меняла воркера. Отмечены те, что ещё не менялись: {len(new)}")
    return text if new else text + "\nВсе уже менялись — отметьте вручную."


def _workers_kb(labels: list[str], picked: list[int], page: int):
    return cancel_kb(PickWorkers.marks, marks_kb("wpick", labels, picked, page, [
        ("☑️ Все", "all"), ("⬜ Никого", "none"), ("🆕 Ещё не менялись", "new"), ("▶️ Далее", "go")], (3, 1)))


async def _ask_workers(message: Message, state: FSMContext, key: str, pool: WorkerPool):
    """Ask which workers the function `key` runs on, the ones it has not changed yet ticked
    (see modules.profile_done), then go on to its _NEXT step."""
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

    await state.clear()  # an abandoned flow's data must not reach this one
    await state.set_state(PickWorkers.marks)
    await state.update_data(workers=None, pick_key=key, pick_paths=[account.path for account in accounts],
                            pick_labels=labels, pick_new=new, picked=new, pick_page=0)
    await message.edit_text(_workers_text(key, new, len(labels), new),  # in its screen's place
                            reply_markup=_workers_kb(labels, new, 0))


@router.callback_query(PickWorkers.marks, ChoiceCB.filter(F.scope == "wpick"))
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
        # the paths, «all» too: a worker added meanwhile (by phone, tdata) was never shown nor picked
        await state.update_data(workers=[paths[i] for i in picked])
        title = BOT_FUNCTIONS_BY_KEY[data["pick_key"]].title
        await callback.message.edit_text(f"{title} — воркеры: {len(picked)} из {len(paths)}")
        await _NEXT[data["pick_key"]](callback.message, state,
                                      dict(pool=pool, functions=functions, manager=manager))
        return

    await callback.answer()
    new_picked, new_page = marks_step(value, picked, page, len(paths),
                                      {"all": range(len(paths)), "none": [], "new": new})
    if (new_picked, new_page) == (picked, page):  # an unchanged message can't be edited ("page" too)
        return
    await state.update_data(picked=new_picked, pick_page=new_page)
    await callback.message.edit_text(_workers_text(data["pick_key"], new_picked, len(labels), new),
                                     reply_markup=_workers_kb(labels, new_picked, new_page))


async def _picked(state: FSMContext) -> list[str] | None:
    """The session paths of the workers picked for the job."""
    return (await state.get_data()).get("workers")


# --- running a function on the picked workers ---

# the job's progress header, by function key (photo and 2FA run with their own)
_HEADERS = {"bio": "Смена bio…", "name": "Смена имени…", "username": "Смена username…",
            "clearchannel": "Очистка канала в профиле…", "lastseen": "Скрытие последнего визита…",
            "terminate": "Сброс чужих сессий…"}


async def _run(key: str, message: Message, pool: WorkerPool, functions: dict, manager: JobManager, only,
               **kwargs):
    """The function's run(report, **kwargs) on the picked workers (`only`)."""
    instance, bot_function = resolve(functions, key)
    await manager.run(
        message.bot, message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(r, **kwargs),
        _HEADERS[key],
        only=only,
    )


def _no_input(key: str):
    """The step after the worker picker of a function that asks nothing more: run it on them."""
    async def run(message: Message, state: FSMContext, deps: dict):
        only = await _picked(state)
        await state.clear()
        await _run(key, message, deps["pool"], deps["functions"], deps["manager"], only)
    return run


# --- name, username, bio: typed, a sent .txt or the list in assets/ ---

@dataclass(frozen=True)
class _TextField:
    prompt: str
    path: str  # its list in assets/, offered by button
    typed: Callable[[str], dict]  # the run's kwargs from the typed text
    listed: Callable[[list[str]], dict]  # ...from a list: each worker gets one of them


def _full_name(name: str) -> dict:
    parts = name.split(maxsplit=1)
    return dict(first_name=parts[0], last_name=parts[1] if len(parts) == 2 else None)


TEXT_FIELDS = {
    "name": _TextField("Пришлите имя для выбранных аккаунтов (например «Иван Петров») — или .txt со списком "
                       "имён, по одному на строку: каждый аккаунт получит случайное.",
                       NAMES_FILE, _full_name, lambda names: dict(names=names)),
    "username": _TextField("Пришлите основу для username (если занята — добавится случайное число: base24, "
                           "base3071, …) — или .txt со списком username, по одному на строку: аккаунты получат их "
                           "по порядку.",
                           USERNAMES_FILE, lambda base: dict(base=base),
                           lambda usernames: dict(usernames=[u.lstrip("@") for u in usernames])),
    "bio": _TextField("Пришлите новый текст bio для выбранных аккаунтов — или .txt со списком вариантов, "
                      "по одному на строку: каждый аккаунт получит случайный.",
                      BIOS_FILE, lambda bio: dict(bio=bio), lambda bios: dict(bios=bios)),
}


def _text_prompt(key: str):
    """The step after the worker picker of name, username and bio."""
    async def prompt(message: Message, state: FSMContext, deps: dict):
        field = TEXT_FIELDS[key]
        await state.set_state(ProfileText.text)
        await message.answer(field.prompt, reply_markup=cancel_kb(ProfileText.text, _file_kb("text_file", field.path)))
    return prompt


async def _run_picked(message: Message, state: FSMContext, pool: WorkerPool, functions: dict, manager: JobManager,
                      **kwargs):
    """The text's function (its key: the picker's) on the picked workers."""
    data = await state.get_data()
    await state.clear()
    await _run(data["pick_key"], message, pool, functions, manager, data.get("workers"), **kwargs)


@router.callback_query(ProfileText.text, ChoiceCB.filter(F.scope == "text_file"))
async def text_from_file(callback: CallbackQuery, state: FSMContext, pool: WorkerPool, functions: dict,
                         manager: JobManager):
    await callback.answer()
    field = TEXT_FIELDS[(await state.get_data())["pick_key"]]
    if lines := _file_lines(field.path):
        await _run_picked(callback.message, state, pool, functions, manager, **field.listed(lines))


@router.message(ProfileText.text, F.document)
async def text_list(message: Message, state: FSMContext, pool: WorkerPool, functions: dict, manager: JobManager):
    lines = await _sent_lines(message)
    if lines is not None:
        field = TEXT_FIELDS[(await state.get_data())["pick_key"]]
        await _run_picked(message, state, pool, functions, manager, **field.listed(lines))


@router.message(ProfileText.text)
async def text_typed(message: Message, state: FSMContext, pool: WorkerPool, functions: dict, manager: JobManager):
    text = await require_text(message)
    if text is not None:
        field = TEXT_FIELDS[(await state.get_data())["pick_key"]]
        await _run_picked(message, state, pool, functions, manager, **field.typed(text))


# --- change profile photo ---

async def _photo_prompt(message: Message, state: FSMContext, deps: dict):
    await state.set_state(ChangePhoto.photo)
    await message.answer(
        "Пришлите картинку (фото или файлом) — она станет аватаром выбранных аккаунтов, прежние аватарки удалятся:",
        reply_markup=cancel_kb(ChangePhoto.photo, choice_kb("photo_src", [("📄 Из assets/photos", "folder")])),
    )


@router.callback_query(ChangePhoto.photo, ChoiceCB.filter(F.scope == "photo_src"))
async def photo_folder(callback: CallbackQuery, state: FSMContext, pool: WorkerPool, functions: dict,
                       manager: JobManager):
    await callback.answer()
    only = await _picked(state)
    await state.clear()
    instance, bot_function = resolve(functions, "photo")
    await manager.run(
        callback.bot, callback.message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(r),
        "Смена фото (из assets/photos/)…",
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
        await back_answer(message, f"⚠️ Не удалось скачать картинку: {err}", PROFILE, manager.project_id)
        return

    instance, bot_function = resolve(functions, "photo")

    await manager.run(
        message.bot, message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(r, photo_path=path),
        "Смена фото…",
        cleanup=lambda: os.remove(path),
        only=only,
    )


# --- clear personal channel (no input) ---

# --- hide last seen (no input) ---

# --- 2fa ---

async def _twofa_prompt(message: Message, state: FSMContext, deps: dict):
    await state.set_state(SetPassword.password)
    await message.answer("🔒 Пришлите новый пароль 2FA (сообщение удалю):",
                         reply_markup=cancel_kb(SetPassword.password))


@router.message(SetPassword.password)
async def twofa_run(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Нужен текст. Пришлите ещё раз:")
        return
    password = message.text
    await delete_quietly(message)
    if not password.strip():  # as the CLI: an empty one would mean no 2FA
        await message.answer("Пароль не может быть пустым. Пришлите новый пароль 2FA:")
        return
    # typed unseen and deleted: asked twice, as the CLI does, so a typo isn't set on every worker
    await state.update_data(new_password=password)
    await state.set_state(SetPassword.confirm)
    await message.answer("Пришлите пароль 2FA ещё раз — для проверки:", reply_markup=cancel_kb(SetPassword.confirm))


@router.message(SetPassword.confirm)
async def twofa_confirm(message: Message, state: FSMContext, pool: WorkerPool, functions: dict,
                        manager: JobManager):
    if not message.text:
        await message.answer("Нужен текст. Пришлите ещё раз:")
        return
    await delete_quietly(message)
    password = (await state.get_data()).get("new_password")
    if message.text != password:
        await state.set_state(SetPassword.password)
        await message.answer("Пароли не совпадают. Пришлите новый пароль 2FA:",
                             reply_markup=cancel_kb(SetPassword.password))
        return
    only = await _picked(state)
    await state.clear()
    instance, bot_function = resolve(functions, "2fa")
    await manager.run(
        message.bot, message.chat.id, pool, instance, bot_function,
        lambda f, r: f.run(password, r),
        "Установка 2FA…",
        only=only,
    )


# --- terminate other sessions (no input) ---

# the step after the worker picker, by function key
_NEXT = {
    **{key: _text_prompt(key) for key in TEXT_FIELDS},
    "photo": _photo_prompt,
    "clearchannel": _no_input("clearchannel"),
    "lastseen": _no_input("lastseen"),
    "2fa": _twofa_prompt,
    "terminate": _no_input("terminate"),
}


@router.callback_query(FunctionCB.filter(F.key.in_(_NEXT)))
async def picker_start(callback: CallbackQuery, callback_data: FunctionCB, state: FSMContext, pool: WorkerPool):
    """Every profile and security function begins with the worker picker."""
    if await ensure_workers(callback, pool):
        await _ask_workers(callback.message, state, callback_data.key, pool)
