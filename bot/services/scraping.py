import asyncio
import os

from scraper.scrape import ScrapeParams, run as scrape_run
from scraper.verify import VerifyParams, run as verify_run

TELEGRAM_UPLOAD_LIMIT = 50 * 1024 * 1024  # bot send_document limit


def worker_session(pool, index: int = 0):
    """A worker account's client (never the host) for the scraper; None if no workers."""
    workers = pool.workers
    if not workers:
        return None

    index = max(0, min(index, len(workers) - 1))
    return workers[index]


def dir_snapshot(path) -> dict:
    """{name: mtime_ns}: a re-run overwrites same-named files, so compare mtimes too."""
    if not os.path.isdir(path):
        return {}
    snapshot = {}
    for name in os.listdir(path):
        try:
            snapshot[name] = os.stat(os.path.join(path, name)).st_mtime_ns
        except OSError:  # a dangling symlink / a file removed meanwhile: not an output file
            pass
    return snapshot


def new_files(path, before: dict) -> list[str]:
    after = dir_snapshot(path)
    paths = (os.path.join(path, name) for name, mtime in after.items() if before.get(name) != mtime)
    return sorted(p for p in paths if os.path.isfile(p))  # skip dirs (e.g. <name>_partial)


async def do_scrape(credentials, params: ScrapeParams):
    return await asyncio.to_thread(scrape_run, credentials, params)


async def do_verify(credentials, params: VerifyParams):
    return await asyncio.to_thread(verify_run, credentials, params)
