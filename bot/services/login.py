"""The bot's side of signing in (the logic: modules/login.py): the login codes of a pool worker
or a personal account, read so that a job, the auto-reply or a scrape using it is not cut off,
and a sign-in by phone waiting for its code — at most one per chat in _logins, closed on success,
failure or after LOGIN_TIMEOUT.
"""
import asyncio
from datetime import datetime

from modules import login as sign_in
from modules.login import Login
from bot.services.delegation import borrow

LOGIN_TIMEOUT = 600           # an abandoned sign-in's client is closed after this


async def fetch_codes(pool, client, path: str) -> list[tuple[str, datetime]]:
    """The account's recent login codes. An account a job or the auto-reply uses stays connected."""
    return await borrow(pool, client, path, lambda: sign_in.read_codes(client), sign_in.FETCH_TIMEOUT)


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
