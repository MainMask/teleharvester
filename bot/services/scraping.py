import asyncio
import os
import re

from scraper.scrape import ScrapeParams, run as scrape_run
from scraper.verify import VerifyParams, run as verify_run

TELEGRAM_UPLOAD_LIMIT = 50 * 1024 * 1024  # bot send_document limit


def parse_channels(raw: str) -> list[str]:
    return [c.strip() for c in re.split(r"[,\s]+", raw) if c.strip()]


def worker_session_string(pool, index: int = 0):
    """StringSession of a worker account (never the host); None if no workers.

    `client.session.save()` serialises the key without opening a connection.
    """
    workers = pool.workers
    if not workers:
        return None

    index = max(0, min(index, len(workers) - 1))
    return workers[index].session.save()


def dir_snapshot(path) -> set:
    return set(os.listdir(path)) if os.path.isdir(path) else set()


def new_files(path, before: set) -> list[str]:
    after = dir_snapshot(path)
    return sorted(os.path.join(path, name) for name in (after - before))


async def do_scrape(credentials, params: ScrapeParams):
    return await asyncio.to_thread(scrape_run, credentials, params)


async def do_verify(credentials, params: VerifyParams):
    return await asyncio.to_thread(verify_run, credentials, params)
