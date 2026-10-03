import asyncio
import html
import io
import shutil
import tempfile
import zipfile

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import MenuAction, MenuCB
from bot.keyboards.menu import accounts_kb, main_menu
from bot.routers._common import ensure_workers, require_text
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from bot.states import ImportTdata, SetProxy
from modules import tdata_import
from modules.types.proxy import ACCOUNTS_PER_PROXY, parse_proxies

router = Router()

GET_ME_TIMEOUT = 20  # seconds per worker when polling profiles for the accounts list
MAX_MESSAGE = 4000   # keep each chunk under Telegram's 4096-char limit

MAX_PROXY_FILE_SIZE = 256 * 1024       # a proxy list is tiny; reject anything clearly wrong
MAX_TDATA_ZIP_SIZE = 10 * 1024 * 1024  # a tdata archive is small; cap at 10 MB

PROXY_PROMPT = (
    "Пришлите список прокси — <b>.txt файлом</b> или текстом, по одному на строку:\n"
    "<code>scheme://user:pass@ip:port</code> (socks5/socks4/http).\n\n"
    f"Они распределятся по аккаунтам: один прокси на {ACCOUNTS_PER_PROXY} аккаунта."
)


async def _worker_line(storage, client, index: int) -> str:
    """One account's line for the list; live get_me(), falling back to stored data."""
    async def fetch_me():
        async with storage.ainitialize_session(client):
            return await client.get_me()

    try:
        # the timeout covers connect() too: a dead proxy must not stall the whole list
        me = await asyncio.wait_for(fetch_me(), GET_ME_TIMEOUT)
    except Exception:
        js = storage.jsessions_paths.get(storage.get_session_path(client))
        if js is not None:
            account = js.account.account
            name = " ".join(filter(None, [account.first_name, account.last_name])) or "—"
            return (f"{index}. {html.escape(name)} — +{html.escape(str(account.phone_number))} — "
                    f"ID {account.user_id} — ⚠️ не удалось опросить")
        return f"{index}. ⚠️ не удалось опросить (StringSession)"

    name = " ".join(filter(None, [me.first_name, me.last_name])) or "—"
    username = f"@{me.username}" if me.username else "—"
    return f"{index}. {html.escape(name)} — {html.escape(username)} — ID {me.id}"


async def _send_chunked(message: Message, header: str, lines: list[str]):
    """Send header + lines in message-sized chunks; keyboard goes on the last one."""
    chunks, current = [], header
    for line in lines:
        if len(current) + len(line) + 1 > MAX_MESSAGE:
            chunks.append(current)
            current = ""
        current += ("\n" if current else "") + line
    chunks.append(current)

    for i, chunk in enumerate(chunks):
        await message.answer(
            chunk, parse_mode="HTML",
            reply_markup=accounts_kb() if i == len(chunks) - 1 else None,
        )


@router.message(F.text == "👥 Аккаунты")
async def accounts(message: Message, pool: WorkerPool, manager: JobManager):
    count = pool.count()

    if count == 0:
        await message.answer(
            "👥 <b>Аккаунты</b>\n\n"
            "🛡 Хост (этот бот): рискованные действия заблокированы.\n"
            "🤖 Воркеров: <b>0</b>\n\n"
            "Добавьте сессии в <code>sessions/</code>, чтобы запускать задачи.",
            parse_mode="HTML", reply_markup=accounts_kb(),
        )
        return

    if not manager.acquire("Просмотр аккаунтов", cancelable=False, timeout=0):
        await message.answer(
            f"👥 <b>Аккаунты</b> — всего: <b>{count}</b>\n\n"
            f"⛔ Идёт задача «{manager.label}». Детали аккаунтов — после её завершения.",
            parse_mode="HTML", reply_markup=accounts_kb(),
        )
        return

    try:
        workers = pool.workers
        lines = await asyncio.gather(*[
            _worker_line(pool.storage, client, i + 1)
            for i, client in enumerate(workers)
        ])
    finally:
        manager.release()

    header = f"👥 <b>Аккаунты</b> — всего: <b>{count}</b>\n"
    await _send_chunked(message, header, list(lines))


@router.callback_query(MenuCB.filter(F.action == MenuAction.PROXY))
async def proxy_start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool, manager: JobManager):
    await callback.answer()

    if not await ensure_workers(callback, pool):
        return

    if manager.active:
        await callback.message.answer(f"⛔ Занят: {manager.label}. Дождитесь завершения задачи.")
        return

    await state.set_state(SetProxy.input)
    await callback.message.answer(PROXY_PROMPT, parse_mode="HTML")


@router.message(SetProxy.input)
async def proxy_apply(message: Message, state: FSMContext, pool: WorkerPool, manager: JobManager):
    if message.document is not None:
        if (message.document.file_size or 0) > MAX_PROXY_FILE_SIZE:
            await message.answer("Файл слишком большой для списка прокси. Пришлите .txt поменьше.")
            return
        buffer = await message.bot.download(message.document)
        raw = buffer.read().decode("utf-8", "replace")
    else:
        raw = message.text or ""

    try:
        proxies = parse_proxies(raw)
    except ValueError as err:
        await message.answer(f"Не удалось разобрать прокси: {err}. Исправьте и пришлите снова.")
        return

    if not proxies:
        await message.answer("Пустой список. Пришлите хотя бы один прокси.")
        return

    if manager.active:  # a job may have started between the prompt and the reply
        await message.answer(f"⛔ Занят: {manager.label}. Дождитесь завершения задачи.")
        return

    try:
        summary = pool.storage.apply_proxies(proxies)
    except ValueError as err:
        await message.answer(f"⚠️ {err}. Пришлите больше прокси.")
        return

    with open(tdata_import.PROXIES_FILE, "w", encoding="utf-8") as fileobj:
        fileobj.write("\n".join(p.strip() for p in raw.splitlines() if p.strip()) + "\n")

    await state.clear()

    text = (
        f"✅ Прокси применены к <b>{summary['accounts']}</b> аккаунтам "
        f"(использовано прокси: {summary['proxies_used']})."
    )
    if summary["string_sessions_skipped"]:
        text += f"\n⚠️ Пропущено .session-аккаунтов (без метаданных): {summary['string_sessions_skipped']}."

    await message.answer(text, parse_mode="HTML")


# --- tdata upload ---------------------------------------------------------

@router.callback_query(MenuCB.filter(F.action == MenuAction.TDATA))
async def tdata_start(callback: CallbackQuery, state: FSMContext, manager: JobManager):
    await callback.answer()

    if manager.active:
        await callback.message.answer(f"⛔ Занят: {manager.label}. Дождитесь завершения задачи.")
        return

    await state.set_state(ImportTdata.archive)
    await callback.message.answer(
        "Пришлите <b>ZIP-архив с одной папкой tdata</b> (Telegram Desktop).",
        parse_mode="HTML",
    )


@router.message(ImportTdata.archive)
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
    await message.answer("Введите пароль 2FA для аккаунта («-», если пароля нет):")


@router.message(ImportTdata.password)
async def tdata_password(message: Message, state: FSMContext, pool: WorkerPool, manager: JobManager):
    raw = await require_text(message)  # a sticker/photo must not import with "no password"
    if raw is None:
        return
    password = None if raw == "-" else raw

    data = await state.get_data()
    zip_bytes = data.get("zip")
    if not zip_bytes:
        await state.clear()
        await message.answer("Архив потерялся, начните заново.", reply_markup=main_menu())
        return

    if not manager.acquire("Импорт tdata", cancelable=False, timeout=0):
        await message.answer(f"⛔ Занят: {manager.label}. Дождитесь завершения задачи.")
        return

    await state.clear()
    tmp_dir = tempfile.mkdtemp(prefix="tdata_")

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            archive.extractall(tmp_dir)

        tdata_dir = tdata_import.find_tdata_dir(tmp_dir)
        if tdata_dir is None:
            await message.answer("В архиве не найдена папка tdata.", reply_markup=main_menu())
            return

        used = [js.account.proxy for js in pool.storage.jsessions_paths.values()]
        try:
            proxy = tdata_import.choose_pool_proxy(used, tdata_import.load_proxies(tdata_import.PROXIES_FILE))
        except ValueError as err:
            await message.answer(f"⚠️ {err}.", reply_markup=main_menu())
            return

        await message.answer("Импортирую аккаунт…")
        phone = await tdata_import.convert(tdata_dir, proxy, password, "sessions")

        if phone is None:
            await message.answer("⚠️ Аккаунт не авторизован — импорт отменён.", reply_markup=main_menu())
            return

        session = pool.storage.add_jsession(f"sessions/{phone}.jsession")
        if session is None:
            await message.answer(
                f"ℹ️ Аккаунт +{phone} уже есть в sessions/ — файл не изменён.",
                reply_markup=main_menu(),
            )
            return

        # read back from the file: if it was already on disk, convert() kept its settings
        saved = session.account
        proxy_note = (f"{saved.proxy.proxy_type}://{saved.proxy.ip}:{saved.proxy.port}"
                      if saved.proxy else "без прокси")
        await message.answer(
            f"✅ Аккаунт импортирован.\n"
            f"📱 Номер: +{phone}\n"
            f"🔐 2FA: {'да' if saved.password else 'нет'}\n"
            f"🌐 Прокси: {proxy_note}\n"
            f"🤖 Всего воркеров: {pool.count()}",
            reply_markup=main_menu(),
        )
    except Exception as err:
        await message.answer(f"⚠️ Ошибка импорта: {err}", reply_markup=main_menu())
    finally:
        manager.release()
        shutil.rmtree(tmp_dir, ignore_errors=True)
