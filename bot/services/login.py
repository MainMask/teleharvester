"""Sign-in for the bot: a worker's login codes from Telegram, and adding an account by phone.

Login codes: Telegram (777000) sends them to the worker's own chats; the bot reads the
recent ones on request. Adding by phone: the code is requested from a fresh client with a
random official app's device (as sessions/add_session.py), and the signed-in account is
saved as sessions/<phone>.jsession and joins the pool. A sign-in waiting for its code lives
in _logins, at most one per chat, closed on success, failure or after LOGIN_TIMEOUT.
"""
import asyncio
import os
import random
import re
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient
from telethon.sessions import StringSession

from modules.generators.linux import LinuxAPI
from modules.generators.telegram_android import TelegramAppAPI
from modules.storages.sessions_storage import connect_client, release_client
from modules.types.account_settings import AccountSettings

SERVICE_ID = 777000           # Telegram's service account: it sends the login codes
CODE_RE = re.compile(r"(?<!\d)\d{5,6}(?!\d)")
CODES_WINDOW = timedelta(minutes=15)  # older codes have expired
DIALOGS_LIMIT = 50            # a fresh code puts the service chat on top
FETCH_TIMEOUT = 20            # a dead proxy must not hang the button
LOGIN_TIMEOUT = 600           # an abandoned sign-in's client is closed after this
GENERATORS = (LinuxAPI, TelegramAppAPI)


# --- login codes ---------------------------------------------------------------

def extract_code(text: str | None) -> str | None:
    match = CODE_RE.search(text or "")
    return match.group() if match else None


def spaced(code: str) -> str:
    """1 2 3 4 5: a code sent through Telegram whole may be expired by Telegram."""
    return " ".join(code)


def recent_codes(messages, now: datetime) -> list[tuple[str, datetime]]:
    """(code, sent at) of the messages carrying a code sent within CODES_WINDOW, newest first."""
    codes = []
    for message in messages:
        code = extract_code(message.message)
        if code and now - message.date <= CODES_WINDOW:
            codes.append((code, message.date))
    return codes


async def fetch_codes(pool, client, path: str) -> list[tuple[str, datetime]]:
    """The worker's recent login codes. A worker a job or the auto-reply uses stays connected."""
    async def fetch():
        await connect_client(client)  # a no-op if a job already has it connected
        # through the dialogs: a fresh session doesn't know the service account's access hash
        async for dialog in client.iter_dialogs(limit=DIALOGS_LIMIT):
            if dialog.entity.id == SERVICE_ID:
                return await client.get_messages(dialog.entity, limit=5)
        return []

    try:
        messages = await asyncio.wait_for(fetch(), FETCH_TIMEOUT)
    finally:
        if not pool.busy(path):
            await release_client(client)  # the cache would grow every use
    return recent_codes(messages, datetime.now(timezone.utc))


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


def save_account(login, me, sessions_dir: str = "sessions") -> str:
    """Write the signed-in account to sessions/<phone>.jsession; returns its path."""
    path = os.path.join(sessions_dir, f"{me.phone}.jsession")
    os.makedirs(sessions_dir, exist_ok=True)
    AccountSettings.from_client(login.client, me, login.proxy, login.password, login.generator.lang_pack).save(path)
    return path


class Login:
    """One chat's sign-in waiting for its code (and maybe a 2FA password)."""

    def __init__(self, client, phone: str, proxy, generator):
        self.client, self.phone, self.proxy, self.generator = client, phone, proxy, generator
        self.password = None
        self.timer = None


_logins: dict[int, Login] = {}


def get(chat_id: int) -> Login | None:
    return _logins.get(chat_id)


def open_login(chat_id: int, login: Login):
    """Register a sign-in that got its code request through (the caller closes the chat's old one first)."""
    _logins[chat_id] = login
    login.timer = asyncio.create_task(_expire(chat_id, login))


async def close(chat_id: int):
    """Drop the chat's sign-in and disconnect its client (no-op without one)."""
    login = _logins.pop(chat_id, None)
    if login is None:
        return
    if login.timer is not None and login.timer is not asyncio.current_task():
        login.timer.cancel()
    try:
        await login.client.disconnect()
    except Exception:
        pass


async def _expire(chat_id: int, login: Login):
    await asyncio.sleep(LOGIN_TIMEOUT)
    if _logins.get(chat_id) is login:
        await close(chat_id)
