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
from bot.keyboards.menu import WORKERS_BUTTON, main_menu, workers_kb
from bot.routers._common import ensure_workers, require_text
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from bot.states import ImportTdata, SetDelay, SetProfilePause, SetProxy
from functions.base.base import BaseFunction
from modules import restricted_workers, tdata_import
from modules.storages.sessions_storage import profile_order
from modules.settings import Settings
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


def _classify(path, forever: set, status: dict) -> tuple[int, str]:
    """The last @SpamBot check's result: (group, "\\n<mark for the card>"); the list goes
    working first (0), then restricted until a date (1), permanently restricted last (2)."""
    if path in forever:
        return 2, "\n⛔ ограничен бессрочно"
    if path not in status:
        return 0, "\n❔ не проверялся"
    if status[path] == "active":
        return 0, "\n✅ без ограничений"
    return 1, f"\n🚫 ЛС ограничены до {html.escape(status[path])}"


def _worker_line(storage, client, me, index: int, busy: bool = False, mark: str = "") -> str:
    """One account's card for the list; falls back to stored data when get_me() failed
    or was not asked (busy: the scraper runs on it)."""
    if me is None:
        status = "⏳ занят скрапом" if busy else "⚠️ не удалось опросить"
        js = storage.jsessions_paths.get(storage.get_session_path(client))
        if js is not None:
            account = js.account.account
            name = " ".join(filter(None, [account.first_name, account.last_name])) or "—"
            return (f"<b>{index}. {html.escape(name)}</b>\n"
                    f"📱 <code>+{html.escape(str(account.phone_number))}</code> · "
                    f"🆔 <code>{account.user_id}</code>\n"
                    f"{status}{mark}")
        return f"<b>{index}.</b> {status} (StringSession){mark}"

    name = " ".join(filter(None, [me.first_name, me.last_name])) or "—"
    username = f"@{html.escape(me.username)}" if me.username else "—"  # plain: Telegram links it
    return (f"<b>{index}. {html.escape(name)}</b>\n"
            f"👤 {username} · 🆔 <code>{me.id}</code>{mark}")


async def _send_chunked(message: Message, header: str, lines: list[str]):
    """Send header + cards (blank line between) in message-sized chunks."""
    chunks, current = [], header
    for line in lines:
        if len(current) + len(line) + 2 > MAX_MESSAGE:
            chunks.append(current)
            current = ""
        current += ("\n\n" if current else "") + line
    chunks.append(current)

    for chunk in chunks:
        await message.answer(chunk, parse_mode="HTML")


WORKERS_HELP = (
    "<b>📋 Список аккаунтов</b> — имя, username и номер каждого воркера, ограничения от @SpamBot\n"
    "<b>👤 Профиль</b> — имя, username, bio, фото, видимость\n"
    "<b>🔐 Безопасность</b> — 2FA и сброс чужих сессий\n"
    "<b>🩺 Проверка и статистика</b> — ограничения от @SpamBot, страны номеров, очистка\n"
    "<b>🌐 Прокси</b> — раздать прокси всем аккаунтам\n"
    "<b>⏱ Задержка</b> — пауза между действиями воркера в рассылках и вступлениях\n"
    "<b>💬 Автоответ</b> — ответ тем, кто ответил на рассылку в ЛС, и их сообщения сюда\n"
    "<b>📲 Добавить по номеру</b> — войти в аккаунт по номеру и коду\n"
    "<b>🔑 Код входа</b> — коды от Telegram, пришедшие воркеру (для входа с другого устройства)\n"
    "<b>📥 Загрузить tdata</b> — добавить аккаунт из Telegram Desktop"
)


def _workers_screen(pool: WorkerPool) -> str:
    text = f"🤖 <b>Воркеры</b> — подключено: <b>{pool.count()}</b>"
    if pool.count() == 0:
        text += "\n\nЗагрузите tdata или добавьте сессии в <code>sessions/</code>, чтобы запускать задачи."
    return f"{text}\n\n{WORKERS_HELP}"


@router.message(F.text == WORKERS_BUTTON)
async def workers(message: Message, state: FSMContext, pool: WorkerPool):
    await state.clear()
    await message.answer(_workers_screen(pool), parse_mode="HTML", reply_markup=workers_kb())


@router.callback_query(MenuCB.filter(F.action == MenuAction.WORKERS))
async def back_to_workers(callback: CallbackQuery, state: FSMContext, pool: WorkerPool):
    await state.clear()
    await callback.message.edit_text(_workers_screen(pool), parse_mode="HTML", reply_markup=workers_kb())
    await callback.answer()


@router.callback_query(MenuCB.filter(F.action == MenuAction.LIST))
async def list_accounts(callback: CallbackQuery, pool: WorkerPool, manager: JobManager):
    await callback.answer()
    await accounts(callback.message, pool, manager)


async def accounts(message: Message, pool: WorkerPool, manager: JobManager):
    count = pool.count()

    if count == 0:
        await message.answer(
            "👥 <b>Аккаунты</b>\n\n"
            "🤖 Воркеров: <b>0</b>\n\n"
            "Загрузите tdata или добавьте сессии в <code>sessions/</code>, чтобы запускать задачи.",
            parse_mode="HTML",
        )
        return

    if not manager.acquire("Просмотр аккаунтов", cancelable=False, timeout=0):
        await message.answer(
            f"👥 <b>Аккаунты</b> — всего: <b>{count}</b>\n\n"
            f"⛔ Идёт задача «{manager.label}». Детали аккаунтов — после её завершения.",
            parse_mode="HTML",
        )
        return

    # the scraper's worker isn't polled: its auth key is in use by the scrape's own client, and
    # a second connection from another IP (a rotating proxy) risks AUTH_KEY_DUPLICATED
    scraping = pool.scraping.path if pool.scraping is not None else None

    def busy(client) -> bool:
        return scraping is not None and pool.storage.get_session_path(client) == scraping

    async def profile(client):
        return None if busy(client) else await pool.storage.fetch_me(client, GET_ME_TIMEOUT)

    try:
        workers = pool.workers
        pool.in_job = [w for w in workers if not busy(w)]  # polled now: no scrape starts on them
        profiles = await asyncio.gather(*[profile(client) for client in workers])
    finally:
        pool.in_job = []
        manager.release()

    forever, status = set(restricted_workers.load()), restricted_workers.load_status()
    checks = {id(client): _classify(pool.storage.get_session_path(client), forever, status) for client in workers}

    ordered = sorted(zip(workers, profiles), key=lambda pair: (checks[id(pair[0])][0], profile_order(pair[1])))
    lines = [_worker_line(pool.storage, client, me, i + 1, busy(client), checks[id(client)][1])
             for i, (client, me) in enumerate(ordered)]

    header = f"👥 <b>Аккаунты</b> — всего: <b>{count}</b>"
    if restricted := sum(1 for group, _ in checks.values() if group):
        header += f" · ограничены: <b>{restricted}</b>"
    await _send_chunked(message, header, lines)


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
    if pool.polling is not None:  # its old client is connected through the old proxy until the poll ends
        await message.answer("⛔ Идёт проверка автоответа. Пришлите прокси ещё раз через минуту.")
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


# --- delay between actions ---------------------------------------------------

def _delay_text(delay: list) -> str:
    return "–".join(str(part) for part in delay) + " с"


def _parse_seconds(text: str | None) -> list | None:
    """[sec] or [min, max] from "7" / "5-10"; None if it's no such (non-negative) range."""
    try:
        seconds = BaseFunction.parse_delay((text or "").replace(" ", ""))
    except ValueError:
        return None
    if not seconds or len(seconds) > 2 or any(part < 0 for part in seconds):
        return None
    return seconds


@router.callback_query(MenuCB.filter(F.action == MenuAction.DELAY))
async def delay_start(callback: CallbackQuery, state: FSMContext, settings: Settings):
    await callback.answer()
    await state.set_state(SetDelay.input)
    await callback.message.answer(
        f"⏱ Сейчас между действиями воркера: <b>{_delay_text(settings.delay)}</b> "
        "(рассылки, инвайтинг, контакты, вступления, реакции, опросы, жалобы).\n\n"
        "Пришлите новую задержку в секундах: <code>5-10</code> (случайная в диапазоне) "
        "или <code>7</code>.",
        parse_mode="HTML",
    )


@router.message(SetDelay.input)
async def delay_apply(message: Message, state: FSMContext, settings: Settings):
    delay = _parse_seconds(message.text)
    if delay is None:
        await message.answer("Нужно число или диапазон, например 5-10 или 7. Пришлите ещё раз.")
        return

    try:
        settings.set_delay(delay)
    except (OSError, ValueError) as err:  # ValueError: a config.toml broken by hand meanwhile
        await message.answer(f"⚠️ Не удалось сохранить config.toml: {err}")
        return
    await state.clear()
    await message.answer(f"✅ Задержка: {_delay_text(delay)}. Действует сразу и в CLI.", reply_markup=main_menu())


# --- pause between accounts for profile changes ------------------------------

@router.callback_query(MenuCB.filter(F.action == MenuAction.PROFILE_PAUSE))
async def profilepause_start(callback: CallbackQuery, state: FSMContext, settings: Settings):
    await callback.answer()
    await state.set_state(SetProfilePause.input)
    await callback.message.answer(
        f"⏳ Сейчас пауза между аккаунтами при смене профиля: <b>{_delay_text(settings.profile_pause)}</b> "
        "(имя, bio, username, 2FA, фото, канал, last seen).\n\n"
        "Пришлите новую паузу в секундах: <code>60-180</code> (случайная в диапазоне) "
        "или <code>120</code>.",
        parse_mode="HTML",
    )


@router.message(SetProfilePause.input)
async def profilepause_apply(message: Message, state: FSMContext, settings: Settings):
    pause = _parse_seconds(message.text)
    if pause is None:
        await message.answer("Нужно число или диапазон, например 60-180 или 120. Пришлите ещё раз.")
        return

    try:
        settings.set_profile_pause(pause)
    except (OSError, ValueError) as err:  # ValueError: a config.toml broken by hand meanwhile
        await message.answer(f"⚠️ Не удалось сохранить config.toml: {err}")
        return
    await state.clear()
    await message.answer(
        f"✅ Пауза профиля: {_delay_text(pause)}. Действует сразу и в CLI.", reply_markup=main_menu()
    )


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

        # an account already loaded may sit in a file not named after its phone
        session = None if pool.storage.is_phone_exists(phone) else pool.storage.add_jsession(f"sessions/{phone}.jsession")
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
