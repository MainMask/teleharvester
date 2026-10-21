"""📥 A worker from a tdata folder (Telegram Desktop), uploaded as a ZIP (➕ Добавить)."""
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
from bot.routers._common import back_answer, delete_quietly
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager, busy
from bot.states import ImportTdata
from modules import projects, tdata_import
from modules.registry import ACCOUNTS, MAIN_MENU

router = Router()

MAX_TDATA_ZIP_SIZE = 10 * 1024 * 1024  # a tdata archive is small; cap at 10 MB


@router.callback_query(MenuCB.filter(F.action == MenuAction.TDATA))
async def tdata_start(callback: CallbackQuery, state: FSMContext, manager: JobManager):
    await callback.answer()

    if manager.active:
        await back_answer(callback.message, busy(manager.label), ACCOUNTS, manager.project_id)
        return

    await state.set_state(ImportTdata.archive)
    await callback.message.answer(  # a prompt for a file: a new message, ➕ Добавить's menu stays
        "📥 Воркер из tdata — пришлите <b>ZIP-архив с одной папкой tdata</b> (Telegram Desktop).",
        parse_mode="HTML", reply_markup=cancel_kb(ImportTdata.archive),
    )


@router.message(ImportTdata.archive, ~F.text.in_(MAIN_MENU))
async def tdata_archive(message: Message, state: FSMContext):
    document = message.document

    if document is None:
        await message.answer("Ожидается ZIP-файл. Пришлите архив с папкой tdata.")
        return

    if (document.file_size or 0) > MAX_TDATA_ZIP_SIZE:
        await message.answer("Архив слишком большой. tdata весит немного — пришлите ZIP поменьше.")
        return

    buffer = await message.bot.download(document)
    data = buffer.read()

    if not zipfile.is_zipfile(io.BytesIO(data)):
        await message.answer("Это не ZIP-архив. Пришлите папку tdata, упакованную в .zip.")
        return

    await state.update_data(zip=data)
    await state.set_state(ImportTdata.password)
    await message.answer("🔐 Пришлите пароль 2FA аккаунта (сообщение удалю) — или нажмите «⏭ Без пароля»:",
                         reply_markup=cancel_kb(ImportTdata.password,
                                                choice_kb("tdata_nopass", [("⏭ Без пароля", "go")])))


@router.callback_query(ImportTdata.password, ChoiceCB.filter(F.scope == "tdata_nopass"))
async def tdata_no_password(callback: CallbackQuery, state: FSMContext, pool: WorkerPool, manager: JobManager):
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)  # pressed once: one import
    await _import(callback.message, state, pool, manager, None)


@router.message(ImportTdata.password, ~F.text.in_(MAIN_MENU))
async def tdata_password(message: Message, state: FSMContext, pool: WorkerPool, manager: JobManager):
    raw = message.text  # as typed: spaces may be part of it, as at the sign-in by phone
    if not raw:  # a sticker/photo must not import with "no password"
        await message.answer("Нужен пароль текстом — или «⏭ Без пароля» выше. Пришлите ещё раз:")
        return
    await delete_quietly(message)
    await _import(message, state, pool, manager, None if raw.strip() == "-" else raw)  # «-»: as before the button


async def _import(message: Message, state: FSMContext, pool: WorkerPool, manager: JobManager, password: str | None):
    """The uploaded tdata into the project: a worker on one of its proxies."""
    data = await state.get_data()
    zip_bytes = data.get("zip")
    if not zip_bytes:
        await state.clear()
        await message.answer("Архив потерялся, начните заново.", reply_markup=back_kb(ACCOUNTS, manager.project_id))
        return

    if not manager.acquire("📥 Импорт tdata", cancelable=False, timeout=0):
        await back_answer(message, busy(manager.label), ACCOUNTS, manager.project_id)
        return

    await state.clear()
    tmp_dir = None

    try:
        tmp_dir = tempfile.mkdtemp(prefix="tdata_")  # inside try: a full disk must not hold the slot
        try:
            tdata_dir = tdata_import.unpack(zip_bytes, tmp_dir)
        except ValueError as err:
            await message.answer(f"{err}.", reply_markup=back_kb(ACCOUNTS, manager.project_id))
            return
        if tdata_dir is None:
            await message.answer("В архиве не найдена папка tdata.", reply_markup=back_kb(ACCOUNTS, manager.project_id))
            return

        used = [js.account.proxy for js in pool.storage.jsessions_paths.values()]
        try:
            proxy = tdata_import.choose_pool_proxy(
                used, tdata_import.load_proxies(projects.proxies_path(pool.storage.home)))
        except ValueError as err:
            await message.answer(f"⚠️ {err}.", reply_markup=back_kb(ACCOUNTS, manager.project_id))
            return

        await message.answer("Импортирую аккаунт…")
        phone = await tdata_import.convert(tdata_dir, proxy, password, pool.storage.directory)

        if phone is None:
            await message.answer("⚠️ Аккаунт не авторизован — импорт отменён.",
                                 reply_markup=back_kb(ACCOUNTS, manager.project_id))
            return

        # an account already loaded may sit in a file not named after its phone
        session = (None if pool.storage.is_phone_exists(phone)
                   else pool.storage.add_jsession(os.path.join(pool.storage.directory, f"{phone}.jsession")))
        if session is None:
            await message.answer(
                f"ℹ️ Аккаунт +{phone} уже есть в проекте — файл не изменён.",
                reply_markup=back_kb(ACCOUNTS, manager.project_id),
            )
            return

        # read back from the file: if it was already on disk, convert() kept its settings
        saved = session.account
        proxy_note = saved.proxy.label() if saved.proxy else "без прокси"
        await message.answer(
            f"✅ Аккаунт импортирован.\n"
            f"📱 Номер: +{phone}\n"
            f"🔐 2FA: {'да' if saved.password else 'нет'}\n"
            f"🌐 Прокси: {proxy_note}\n"
            f"🤖 Воркеров в проекте: {pool.count()}",
            reply_markup=back_kb(ACCOUNTS, manager.project_id),
        )
    except Exception as err:
        await message.answer(f"⚠️ Ошибка импорта: {err}", reply_markup=back_kb(ACCOUNTS, manager.project_id))
    finally:
        manager.release()
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)
