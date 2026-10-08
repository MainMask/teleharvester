"""Sign-in, for the bot (bot/routers/login.py) and the terminal menu (functions/sign_in.py)
alike: an account's login codes from Telegram, adding an account by phone, removing a personal one.

Login codes: Telegram (777000) sends them to the account's own chats; they are read on request.
Adding by phone: a worker or a personal account (role). The code is requested from a fresh
client with a random official app's device, and the signed-in account is saved as
sessions/<phone>.jsession and joins the pool — a personal one goes to personal_sessions/,
without a proxy and without its 2FA password. The bot's own side (a sign-in waiting for its
code in a chat, the codes of a pool worker) is bot/services/login.py.
"""
import asyncio
import os
import random
import re
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient, errors
from telethon.sessions import StringSession

from modules import scraper_creds, tdata_import
from modules.generators.linux import LinuxAPI
from modules.generators.telegram_android import TelegramAppAPI
from modules.storages.sessions_storage import connect_client
from modules.types.account_settings import AccountSettings

SERVICE_ID = 777000           # Telegram's service account: it sends the login codes
CODE_RE = re.compile(r"(?<!\d)\d{5,6}(?!\d)")
CODES_WINDOW = timedelta(minutes=15)  # older codes have expired
DIALOGS_LIMIT = 50            # a fresh code puts the service chat on top
FETCH_TIMEOUT = 20            # a dead proxy must not hang the button
GENERATORS = (LinuxAPI, TelegramAppAPI)
PERSONAL = "personal"         # the role of an account added by phone; any other: a worker


# --- login codes ---------------------------------------------------------------

def extract_code(text: str | None) -> str | None:
    match = CODE_RE.search(text or "")
    return match.group() if match else None


def spaced(code: str) -> str:
    """1 2 3 4 5: a code sent through Telegram whole may be expired by Telegram."""
    return " ".join(code)


NO_CODES = f"За последние {CODES_WINDOW.seconds // 60} мин кодов нет. Запросите вход на устройстве"


def ago(sent_at: datetime, now: datetime) -> str:
    """When a code came, for the code lists (bot and terminal)."""
    minutes = int((now - sent_at).total_seconds() // 60)
    return "только что" if minutes < 1 else f"{minutes} мин назад"


def recent_codes(messages, now: datetime) -> list[tuple[str, datetime]]:
    """(code, sent at) of the messages carrying a code sent within CODES_WINDOW, newest first."""
    codes = []
    for message in messages:
        code = extract_code(message.message)
        if code and now - message.date <= CODES_WINDOW:
            codes.append((code, message.date))
    return codes


async def read_codes(client) -> list[tuple[str, datetime]]:
    """The recent login codes of a connected client's account."""
    # through the dialogs: a fresh session doesn't know the service account's access hash
    async for dialog in client.iter_dialogs(limit=DIALOGS_LIMIT):
        if dialog.entity.id == SERVICE_ID:
            return recent_codes(await client.get_messages(dialog.entity, limit=5), datetime.now(timezone.utc))
    return []


# --- adding an account by phone --------------------------------------------------

def normalize_phone(text: str | None) -> str | None:
    """The digits of an international number (+7 999 123-45-67 -> 79991234567), or None."""
    digits = re.sub(r"\D", "", text or "")
    return digits if 7 <= len(digits) <= 15 else None


def code_digits(text: str | None) -> str | None:
    """The code typed with separators (1 2 3 4 5 / 1-2-3-4-5) as digits, or None."""
    digits = re.sub(r"\D", "", text or "")
    return digits if 4 <= len(digits) <= 8 else None


def new_client(proxy):
    """A fresh client presenting a random official app's device; (client, generator)."""
    generator = random.choice(GENERATORS)
    lang = generator.system_lang_code()
    client = TelegramClient(
        StringSession(),
        generator.api_id,
        generator.api_hash,
        device_model=generator.device(),
        app_version=generator.app_version(),
        system_version=generator.sdk(),
        lang_code=lang,  # as SessionsStorage.build_jsession_client loads it back
        system_lang_code=lang,
        proxy=proxy.as_telethon() if proxy else None,
    )
    return client, generator


def known(storage, phone: str) -> bool:
    """The number is already loaded there (stored with or without the +)."""
    return storage is not None and bool(storage.is_phone_exists(phone) or storage.is_phone_exists(f"+{phone}"))


def refusal(role: str, phone: str, workers, personal) -> str | None:
    """Why the number gets no code: it is already loaded (as this role or the other one)."""
    if role == PERSONAL:
        if known(personal, phone):
            return f"ℹ️ Аккаунт +{phone} уже есть среди личных."
        if known(workers, phone):
            return f"⛔ +{phone} — воркер: личным аккаунтом он не станет."
        return None
    if known(workers, phone):
        return f"ℹ️ Аккаунт +{phone} уже есть среди воркеров."
    if known(personal, phone):
        return f"⛔ +{phone} — ваш личный аккаунт (personal_sessions/): воркером он не станет."
    return None


def role_proxy(role: str, workers):
    """The new account's proxy: a worker's from the pool, as the tdata import (at most
    ACCOUNTS_PER_PROXY accounts per proxy; ValueError if all are full); a personal account
    has none, as the ones put in by hand — no worker proxy ties it to the workers."""
    if role == PERSONAL:
        return None
    used = [js.account.proxy for js in workers.jsessions_paths.values()]
    return tdata_import.choose_pool_proxy(used, tdata_import.load_proxies(tdata_import.PROXIES_FILE))


async def request_code(client, phone: str):
    """Connect the fresh client and ask for the code (a dead proxy: Telethon would retry for a
    minute or more)."""
    async def request():
        await client.connect()
        return await client.send_code_request(phone)
    return await asyncio.wait_for(request(), FETCH_TIMEOUT * 2)


def send_error(err: Exception) -> str:
    """Why a code was not sent."""
    if isinstance(err, errors.FloodWaitError):
        return f"слишком много попыток, подождите {err.seconds} с"
    if isinstance(err, errors.PhoneNumberInvalidError):
        return "неверный номер"
    if isinstance(err, errors.PhoneNumberBannedError):
        return "номер заблокирован в Telegram"
    return str(err) or type(err).__name__  # a timeout has no text


def already_known(me, workers, personal) -> bool:
    """The signed-in account turned out to be loaded already (under another number format)."""
    personal_ids = {js.account.account.user_id for js in personal.json_sessions} if personal is not None else set()
    return me.id in personal_ids or known(workers, me.phone)


async def log_out(client) -> bool:
    """End this client's own authorization (best effort; the account's other ones stay)."""
    async def end():
        await connect_client(client)  # a no-op for a connected one
        return await client.log_out()

    try:
        return bool(await asyncio.wait_for(end(), FETCH_TIMEOUT))
    except Exception:
        try:
            await client.disconnect()
        except Exception:
            pass
        return False


def store(login, me, role: str, workers, personal) -> str:
    """Save the signed-in account and add it to its storage; returns its path."""
    if role == PERSONAL:
        # a scrape needs the key alone: the main account's 2FA password is not kept in plain text
        path = save_account(login, me, scraper_creds.PERSONAL_DIR, keep_password=False)
        personal.add_jsession(path)
    else:
        path = save_account(login, me)
        workers.add_jsession(path)
    return path


def added_text(login, me, role: str, workers_count: int) -> str:
    """The summary of an account just added."""
    if role == PERSONAL:
        return (f"✅ Личный аккаунт добавлен — для скрапа, воркером он не станет.\n"
                f"📱 Номер: +{me.phone}\n"
                f"🔐 2FA: {'да — пароль не сохранён' if login.password else 'нет'}")
    proxy = login.proxy
    return (f"✅ Аккаунт добавлен.\n"
            f"📱 Номер: +{me.phone}\n"
            f"🔐 2FA: {'да' if login.password else 'нет'}\n"
            f"🌐 Прокси: {f'{proxy.proxy_type}://{proxy.ip}:{proxy.port}' if proxy else 'без прокси'}\n"
            f"🤖 Всего воркеров: {workers_count}")


async def remove_personal(personal, account) -> bool:
    """Drop a personal account: the bot's own authorization on it ends (best effort, True if it
    did) and its file is deleted. The caller checks it isn't in use."""
    personal.forget_session(account.path)  # before any await: nothing picks it meanwhile
    logged_out = await log_out(account.client)
    try:
        os.remove(account.path)
    except FileNotFoundError:  # removed by hand meanwhile: gone all the same
        pass
    return logged_out


def save_account(login, me, sessions_dir: str = "sessions", keep_password: bool = True) -> str:
    """Write the signed-in account to sessions/<phone>.jsession; returns its path."""
    path = os.path.join(sessions_dir, f"{me.phone}.jsession")
    # an account not loaded (e.g. its connect failed at the menu's start) may still be on disk:
    # its file holds a live key, its proxy and 2FA password; as the tdata import, it is kept
    if os.path.exists(path):
        raise FileExistsError(f"{path} уже есть на диске (аккаунт не загружен — проверьте прокси)")
    os.makedirs(sessions_dir, exist_ok=True)
    password = login.password if keep_password else None
    AccountSettings.from_client(login.client, me, login.proxy, password, login.generator.lang_pack).save(path)
    return path


class Login:
    """One sign-in by phone: its client and what it signs in with (maybe a 2FA password too)."""

    def __init__(self, client, phone: str, proxy, generator):
        self.client, self.phone, self.proxy, self.generator = client, phone, proxy, generator
        self.password = None
        self.timer = None  # the bot's expiry of a sign-in waiting for its code (bot/services/login.py)
