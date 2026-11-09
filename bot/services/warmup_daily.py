"""🔥 Прогрев's daily run: a project with «🔁 Ежедневно» on is warmed once a day by itself, at a random time
between FIRST_HOUR and LAST_HOUR (a fixed hour every day would be a pattern), once its job slot is free — a busy
one is tried again on the next round, until LAST_HOUR: past it (the slot busy all evening, the bot started late) the
run waits for the next day. It reports to the chat that switched it on (modules/warmup.py "chat"),
or to an admin. The day it started is kept (its "auto_day"), so a restart doesn't run it twice.
"""
import asyncio
import datetime
import logging
import random

from modules import warmup
from modules.registry import BOT_FUNCTIONS_BY_KEY

log = logging.getLogger(__name__)

ROUND = 300  # seconds between checks
FIRST_HOUR, LAST_HOUR = 10, 21

_task = None
_planned: dict[int, datetime.datetime] = {}  # project id -> today's time of its run


def planned(project_id: int, now: datetime.datetime) -> datetime.datetime:
    """Today's random time of the project's run, picked once a day (🔥 Прогрев shows it)."""
    time = _planned.get(project_id)
    if time is None or time.date() != now.date():
        time = _planned[project_id] = now.replace(hour=random.randint(FIRST_HOUR, LAST_HOUR - 1),
                                                  minute=random.randint(0, 59), second=0, microsecond=0)
    return time


async def run_due(bot, workspaces, admins, now: datetime.datetime):
    """One round: start today's run of every project whose time has come."""
    bot_function = BOT_FUNCTIONS_BY_KEY["warmup"]
    for workspace in workspaces.all():
        home = workspace.pool.storage.home
        settings = warmup.load_settings(home)
        today = now.date().isoformat()
        if not settings["daily"] or settings["auto_day"] == today or now < planned(workspace.project_id, now):
            continue
        if now.hour >= LAST_HOUR:  # not at night: tomorrow's time then
            continue
        if workspace.manager.active or workspace.pool.count() == 0:  # the next round tries again
            continue
        pool = workspace.pool  # not the one the scraper runs on: the warmup runs without it (WorkerPool.delegate)
        if not warmup.due(pool.storage, [path for path in pool.storage.full_sessions if not pool.scraped(path)]):
            settings["auto_day"] = today
            warmup.save_settings(home, settings)
            continue
        chat = settings["chat"] or min(admins)
        instance = workspace.functions[bot_function.classname]()
        if await workspace.manager.run(bot, chat, workspace.pool, instance, bot_function, lambda f, r: f.run(r),
                                       "🔁 Ежедневный прогрев…"):
            # read again: a setting changed in 🔥 while the run was starting must not be written over
            warmup.save_settings(home, {**warmup.load_settings(home), "auto_day": today})


async def _loop(bot, workspaces, admins):
    while True:
        try:
            await run_due(bot, workspaces, admins, datetime.datetime.now())
        except Exception:  # one bad round (a broken warmup.json) must not end the daily runs
            log.exception("daily warmup round")
        await asyncio.sleep(ROUND)


async def start(bot, workspaces, admins):
    global _task
    _task = asyncio.create_task(_loop(bot, workspaces, admins))  # kept referenced: GC would drop it


async def stop():
    if _task is not None:
        _task.cancel()
