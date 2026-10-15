import asyncio
import functools
import logging
import os
import shutil

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramNetworkError, TelegramServerError
from aiogram.types import BotCommand

from modules import instance_lock
from modules.config import load_env
from modules.settings import Settings
from modules.storages.functions_storage import FunctionsStorage
from modules.scraper_creds import PERSONAL_DIR, personal_storage
from modules.storages.sessions_storage import SessionsStorage

from bot.config import BotConfig
from bot.middlewares.auth import AuthMiddleware
from bot.services import autoreply, watchdog
from bot.services.album import AlbumMiddleware
from bot.services.capture import clear_orphan_temp
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from modules.registry import missing_classes
from bot.routers import (
    accounts,
    activity,
    autoreply as autoreply_menu,
    audience,
    broadcasts,
    invite,
    join,
    login,
    menu,
    moderation,
    pm_broadcast,
    profile,
    projects,
    scraping,
    service,
    settings as settings_menu,
    spamblock,
    stale,
)

# The main menu's buttons first (bot/routers/menu.py opens every screen): a button tapped in the middle
# of any form opens its screen instead of being taken as the form's input. The order of the rest is free.
ROUTERS = (
    menu,
    accounts,
    projects,
    settings_menu,
    login,
    autoreply_menu,
    spamblock,
    pm_broadcast,
    invite,
    join,
    profile,
    audience,
    activity,
    moderation,
    service,
    broadcasts,
    scraping,
    stale,  # last: a button nothing above took
)


BOT_API_RETRY = 10  # seconds between Bot API reachability checks at start


async def wait_for_bot_api(bot, delay=BOT_API_RETRY):
    """Wait until the Bot API answers. start_polling's own get_me() has no retry: a network
    outage at start would exit the process, and systemd's StartLimitBurst would then leave
    the unit failed for good. A bad token (TelegramUnauthorizedError) still exits."""
    while True:
        try:
            await bot.me()
            return
        except (TelegramNetworkError, TelegramServerError) as err:
            logging.warning("Bot API unreachable (%s), retrying in %s s", err, delay)
            await asyncio.sleep(delay)


# present while the bot runs: still there at a start, the last run ended without its shutdown
# (a crash, an out-of-memory kill, a power cut), not by systemctl stop/restart
RUNNING_MARKER = os.path.join("stats", "bot_running")


async def announce_start(bot, admins, pool):
    """Bot startup: tell the admins it (re)started — after a crash, say so: systemd restarts it
    silently, and a crash loop would otherwise show only in journald."""
    crashed = os.path.exists(RUNNING_MARKER)
    os.makedirs(os.path.dirname(RUNNING_MARKER), exist_ok=True)
    open(RUNNING_MARKER, "w").close()

    status = ("⚠️ Бот перезапущен после сбоя — подробности: journalctl -u teleharvester-bot"
              if crashed else "🔄 Бот запущен")
    for admin in admins:
        try:
            await bot.send_message(admin, f"{status} · воркеров: {pool.count()}")
        except Exception as err:  # e.g. an admin who never opened the bot
            logging.warning("start notice to %s: %s", admin, err)


async def set_commands(bot):
    """Bot startup: /start, /status and /cancel in the chat's «Меню» button. Without them the bot still works."""
    try:
        await bot.set_my_commands([BotCommand(command="start", description="Главное меню"),
                                   BotCommand(command="status", description="Что сейчас выполняется"),
                                   BotCommand(command="cancel", description="Отменить ввод или остановить задачу")])
    except Exception as err:
        logging.warning("bot commands: %s", err)


async def mark_stopped():
    """Bot shutdown (a clean stop): the next start is not after a crash."""
    try:
        os.remove(RUNNING_MARKER)
    except OSError:
        pass


_watchdog = None  # the watchdog's task, kept referenced (the loop holds tasks by weak ref only)


async def run_bot():
    global _watchdog
    # first: it pings from the first await on, through wait_for_bot_api's possibly long outage too
    _watchdog = asyncio.create_task(watchdog.run())
    load_env()
    config = BotConfig()
    config.validate()

    instance_lock.hold()  # before the sweep below: it would delete a running bot's broadcast media
    clear_orphan_temp()  # sweep broadcast temp dirs a previous run was killed before cleaning
    shutil.rmtree(profile.PHOTO_TMP_DIR, ignore_errors=True)  # ...and the photo a profile job left

    settings = Settings()
    storage = SessionsStorage("sessions", settings.api_id, settings.api_hash, initialize=False)
    functions_storage = FunctionsStorage("functions", storage, settings)

    # reuse the CLI's auto-discovery: class name -> a new instance for each run (resolve), so no run
    # inherits another's state
    functions = {type(instance).__name__: functools.partial(type(instance), storage, settings)
                 for instance, _doc in functions_storage.functions}

    # fail fast with a clear message if the registry references a class that
    # auto-discovery didn't find (e.g. a renamed class), instead of a KeyError
    # deep inside a handler at first use.
    absent = missing_classes(functions)
    if absent:
        raise RuntimeError(
            "modules/registry.py lists function classes not found in functions/: "
            + ", ".join(absent)
            + ". Rename them back or update BOT_FUNCTIONS."
        )

    pool = WorkerPool(storage)

    bot = Bot(config.token)
    dp = Dispatcher()

    dp["pool"] = pool
    dp["functions"] = functions
    dp["settings"] = settings
    dp["manager"] = JobManager()
    # the scraper's jobs run on their own client in their own slot, so a multi-day scrape
    # doesn't hold up the other tasks; personal accounts are scraper-only, never workers
    dp["scrapes"] = JobManager()
    # the analysis reads files in a child process, no account: a mailing or a scrape doesn't hold it up
    dp["analyses"] = JobManager()
    os.makedirs(PERSONAL_DIR, exist_ok=True)  # a personal account added by phone lands there
    dp["personal"] = personal_storage(settings.api_id, settings.api_hash)
    dp["admins"] = config.admins  # who autoreply notifies
    dp.startup.register(announce_start)  # first: before "continuing the scrape"
    dp.startup.register(set_commands)
    dp.startup.register(scraping.resume_after_restart)
    dp.shutdown.register(scraping.stop_for_shutdown)
    dp.startup.register(autoreply.start)
    dp.shutdown.register(autoreply.stop)
    # last: a stop that hangs until systemd kills the bot leaves the marker, so the next start says so
    dp.shutdown.register(mark_stopped)

    dp.update.outer_middleware(AuthMiddleware(config.admins))
    dp.message.outer_middleware(AlbumMiddleware())

    for module in ROUTERS:
        dp.include_router(module.router)

    await wait_for_bot_api(bot)  # before start_polling: no startup handler runs offline
    await dp.start_polling(bot)
