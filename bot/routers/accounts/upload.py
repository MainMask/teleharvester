"""📥 Workers from files (➕ Добавить): ZIPs of a tdata folder (Telegram Desktop, modules/tdata_import.py), Telethon
.session files with their .json (modules/session_import.py), or ZIPs of these. Each message (or album) is imported at
once: its .session ones, then its tdata ones in turn — one without its 2FA .txt asks for its password —; then the
chat waits for the next files, no ➕ Добавить again for each account."""
import html
import io
import os
import shutil
import tempfile
import zipfile

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import ChoiceCB, MenuAction, MenuCB
from bot.keyboards.common import cancel_kb, choice_kb
from bot.keyboards.menu import back_kb
from bot.routers._common import MAX_MESSAGE, back_answer, busy_step, busy_tap, delete_quietly, download, fit_lines
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager, busy
from bot.states import ImportFiles
from modules import login, projects, session_import, tdata_import
from modules.registry import ADD, title

router = Router()

MAX_FILE_SIZE = 20 * 1024 * 1024  # the Bot API gives files up to 20 MB
SUFFIXES = (".zip", ".session", ".json")


@router.callback_query(MenuCB.filter(F.action == MenuAction.FILES))
async def files_start(callback: CallbackQuery, state: FSMContext, manager: JobManager):
    await callback.answer()

    if manager.active:
        await back_answer(callback.message, busy(manager.label), ADD, manager.project_id)
        return

    await state.clear()  # an abandoned flow's data must not reach this one
    await state.set_state(ImportFiles.files)
    await callback.message.answer(  # a prompt for files: a new message, ➕ Добавить's menu stays
        f"<b>{title('files')}</b>\n\nПришлите ZIP с папкой tdata (Telegram Desktop), "
        "файлы <code>.session</code> (Telethon) вместе с <code>.json</code> того же имени — выделите их вместе — "
        "или ZIP с ними; можно несколько сразу. "
        "Пароль 2FA к tdata можно положить в его ZIP файлом .txt (например <code>2fa.txt</code>: пароль или "
        "<code>2fa: пароль</code>); без него бот спросит пароль.",
        parse_mode="HTML", reply_markup=cancel_kb(ImportFiles.files),
    )


async def _received(message: Message, album) -> dict[str, bytes]:
    """The usable files of a message or an album, by name; what is not usable said."""
    files, skipped = {}, []
    for item in album or [message]:
        document = item.document
        name = os.path.basename(document.file_name or "") if document else ""
        if not name.lower().endswith(SUFFIXES):
            skipped.append(f"{name or 'не файл'} — нужны .zip, .session или .json")
        elif (document.file_size or 0) > MAX_FILE_SIZE:
            skipped.append(f"{name} — больше {MAX_FILE_SIZE // 2**20} МБ")
        elif (data := await download(item, document)) is not None:
            files[name] = data
    if skipped:
        await message.answer("Не принято:\n" + "\n".join(f"• {html.escape(line)}" for line in skipped),
                             parse_mode="HTML")
    return files


@router.message(ImportFiles.files)
async def files_sent(message: Message, state: FSMContext, pool: WorkerPool, manager: JobManager, album=None):
    files = await _received(message, album)
    if not files:
        return
    if manager.active:  # the step stays: the files are sent again once that job is over
        await busy_step(message, manager, ADD, "пришлите файлы ещё раз")
        return
    queue, tmp_dir = [], tempfile.mkdtemp(prefix="upload_")
    try:
        for name, data in files.items():
            if not name.lower().endswith(".zip"):
                with open(os.path.join(tmp_dir, name), "wb") as fileobj:
                    fileobj.write(data)
                continue
            folder = os.path.join(tmp_dir, name[:-len(".zip")])
            try:
                if not zipfile.is_zipfile(io.BytesIO(data)):
                    raise ValueError("это не ZIP-архив")
                if tdata_import.unpack(data, folder) is not None:  # a tdata: its own queue, with its password
                    queue.append(data)
                    shutil.rmtree(folder)
            except ValueError as err:
                await message.answer(f"⚠️ {name}: {err}.")
        if session_import.collect(tmp_dir):
            await _import_sessions(message, pool, manager, tmp_dir)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    await _run(message, state, pool, manager, queue)


async def _import_sessions(message: Message, pool: WorkerPool, manager: JobManager, root: str):
    """The .session files under `root` into the project, their outcome said."""
    if not manager.acquire(title("files"), cancelable=False, timeout=0):  # taken since the check: said, the files again
        await message.answer(f"{busy(manager.label)}\nКогда она закончится, пришлите файлы .session ещё раз.")
        return
    try:
        await message.answer("Импортирую .session…")
        added, failed = await session_import.import_all(root, pool.storage)
        lines = [f"✅ Воркеров из .session добавлено: {len(added)}",
                 *(f"• {html.escape(projects.account_label(path))}" for path in added)]
        if failed:
            lines += ["", f"⚠️ Не добавлено: {len(failed)}",
                      *(f"• {html.escape(name)} — {html.escape(why)}" for name, why in failed)]
        count = f"🤖 Воркеров в проекте: {pool.count()}"  # last, whatever the lists above leave out
        await message.answer(f"{fit_lines(lines, MAX_MESSAGE - len(count) - 1)}\n{count}", parse_mode="HTML")
    except Exception as err:
        await message.answer(f"⚠️ Ошибка импорта .session: {html.escape(str(err))}", parse_mode="HTML")
    finally:
        manager.release()


@router.callback_query(ImportFiles.password, ChoiceCB.filter(F.scope == "tdata_nopass"))
async def tdata_no_password(callback: CallbackQuery, state: FSMContext, pool: WorkerPool, manager: JobManager):
    if await busy_tap(callback, manager):
        return
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)  # pressed once: one import
    await _answered(callback.message, state, pool, manager, None)


@router.message(ImportFiles.password)
async def tdata_password(message: Message, state: FSMContext, pool: WorkerPool, manager: JobManager):
    raw = message.text  # as typed: spaces may be part of it, as at the sign-in by phone
    if not raw:  # a sticker/photo must not import with "no password"
        await message.answer("Нужен пароль текстом — или «⏭ Без пароля» выше. Пришлите ещё раз:")
        return
    await delete_quietly(message)
    await _answered(message, state, pool, manager, None if raw.strip() == "-" else raw)  # «-»: as before the button


async def _answered(message: Message, state: FSMContext, pool: WorkerPool, manager: JobManager, password: str | None):
    """The password step answered: the tdata waiting for it, then the rest of the queue."""
    queue = (await state.get_data()).get("queue")
    if not queue:
        await state.clear()
        await message.answer("⚠️ Архив потерялся, начните заново.", reply_markup=back_kb(ADD, manager.project_id))
        return
    await _run(message, state, pool, manager, queue, password, typed=True)


async def _run(message: Message, state: FSMContext, pool: WorkerPool, manager: JobManager, queue: list[bytes],
               password: str | None = None, typed: bool = False):
    """The tdata archives in turn, each with the password of its 2FA .txt — the first with `password` if `typed`
    (the password step's answer). One without a .txt waits at the password step, the rest after it; once all are
    in, the chat waits for the next files."""
    while queue:
        if not typed and (password := tdata_import.zip_2fa_password(queue[0])) is None:
            await state.set_state(ImportFiles.password)
            await state.update_data(queue=queue)
            more = f" (архивов в очереди: {len(queue)})" if len(queue) > 1 else ""
            no_password = choice_kb("tdata_nopass", [("⏭ Без пароля", "go")])
            await message.answer(f"🔐 Пришлите пароль 2FA аккаунта{more} (сообщение удалю) или нажмите «⏭ Без "
                                 "пароля»:", reply_markup=cancel_kb(ImportFiles.password, no_password))
            return
        if not await _import(message, pool, manager, queue[0], password, typed):  # busy: the step stays
            await state.update_data(queue=queue)
            await busy_step(message, manager, ADD,
                            "пришлите пароль ещё раз" if typed else "пришлите архив tdata ещё раз")
            return
        queue, typed = queue[1:], False
    await state.set_state(ImportFiles.files)
    await state.update_data(queue=[])
    await message.answer(f"📥 Пришлите следующие файлы — или «⬅️ {ADD}», чтобы закончить.",
                         reply_markup=back_kb(ADD, manager.project_id))


async def _import(message: Message, pool: WorkerPool, manager: JobManager, zip_bytes: bytes, password: str | None,
                  typed: bool) -> bool:
    """One uploaded tdata into the project: a worker on one of its proxies; its outcome said. False, nothing done,
    while the project's slot is busy."""
    if not manager.acquire("📥 Импорт tdata", cancelable=False, timeout=0):
        return False

    tmp_dir = None
    try:
        tmp_dir = tempfile.mkdtemp(prefix="tdata_")  # inside try: a full disk must not hold the slot
        try:
            tdata_dir = tdata_import.unpack(zip_bytes, tmp_dir)
        except ValueError as err:
            await message.answer(f"⚠️ {err}.")
            return True
        if tdata_dir is None:
            await message.answer("⚠️ В архиве не найдена папка tdata.")
            return True

        try:
            proxy = login.role_proxy("worker", pool.storage)
        except ValueError as err:
            await message.answer(f"⚠️ {err}.")
            return True

        await message.answer("Импортирую аккаунт…" if typed or password is None
                             else "🔐 Пароль 2FA взят из .txt в архиве. Импортирую аккаунт…")
        phone = await tdata_import.convert(tdata_dir, proxy, password, pool.storage.directory)

        if phone is None:
            await message.answer("⚠️ Аккаунт не авторизован — импорт отменён.")
            return True

        # an account already loaded may sit in a file not named after its phone
        session = (None if pool.storage.is_phone_exists(phone)
                   else pool.storage.add_jsession(os.path.join(pool.storage.directory, f"{phone}.jsession")))
        if session is None:
            await message.answer(f"ℹ️ Аккаунт +{phone} уже есть в проекте — файл не изменён.")
            return True

        # read back from the file: if it was already on disk, convert() kept its settings
        saved = session.account
        proxy_note = saved.proxy.label() if saved.proxy else "без прокси"
        await message.answer(
            f"✅ Воркер импортирован.\n"
            f"📱 Номер: +{phone}\n"
            f"🔐 2FA: {'да' if saved.password else 'нет'}\n"
            f"🌐 Прокси: {proxy_note}\n"
            f"🤖 Воркеров в проекте: {pool.count()}",
        )
    except Exception as err:
        await message.answer(f"⚠️ Ошибка импорта: {err}")
    finally:
        manager.release()
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    return True
