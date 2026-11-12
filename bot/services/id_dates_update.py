"""The weekly update of the user id → registration date table (modules/account_age.py): its dataset downloaded from
its repository and used once it is checked (account_age.update); a failure — no network, a bad file — leaves the one
in use and is tried again sooner. Logged only: nothing for the admins to do. The request carries no data of the bot's.
"""
import asyncio
import logging

import aiohttp

from modules import account_age

log = logging.getLogger(__name__)

URL = "https://raw.githubusercontent.com/Jobians/telegram-id-age/main/dataset.json"
MAX_SIZE = 2 * 1024 * 1024  # the dataset is ~15 KB: anything near this is not it
TIMEOUT = 30
FIRST = 300  # seconds after the start: not in the way of starting
WEEK, RETRY = 7 * 86400, 6 * 3600  # after a check done, after a failure

_task = None


async def fetch() -> bytes:
    """The whole file (read(n) would give what has come so far only), at most MAX_SIZE."""
    data = b""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as session:
        async with session.get(URL) as response:
            response.raise_for_status()
            async for chunk in response.content.iter_chunked(65536):
                data += chunk
                if len(data) > MAX_SIZE:
                    raise ValueError("файл слишком большой")
    return data


async def check_once(download=fetch) -> bool:
    """One check: True once done — updated, the same, or refused (its data won't be better in 6 h: a week then) —,
    False after a failure to try again sooner (no network, not written)."""
    try:
        raw = await download()
    except Exception as err:  # a network error, a 404, a timeout (no text)
        log.warning("таблица возрастов не скачана: %s", str(err) or type(err).__name__)
        return False
    try:
        done = account_age.update(raw)
    except ValueError as err:  # no good (bad JSON too): the one in use stays
        log.warning("таблица возрастов отклонена: %s", err)
        return True
    except OSError as err:  # not written: the one in use stays
        log.warning("таблица возрастов не сохранена: %s", err)
        return False
    log.info(done or "таблица возрастов актуальна")
    return True


async def _loop():
    await asyncio.sleep(FIRST)
    while True:
        await asyncio.sleep(WEEK if await check_once() else RETRY)


async def start():
    global _task
    _task = asyncio.create_task(_loop())  # kept referenced: GC would drop it


async def stop():
    if _task is not None:
        _task.cancel()
