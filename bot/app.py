import asyncio
import logging
import os
import shutil

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramNetworkError, TelegramServerError

from modules.config import load_env
from modules.settings import Settings
from modules.storages.functions_storage import FunctionsStorage
from modules.scraper_creds import personal_storage
from modules.storages.sessions_storage import SessionsStorage

from bot.config import BotConfig
from bot.middlewares.auth import AuthMiddleware
from bot.services import autoreply
from bot.services.album import AlbumMiddleware
from bot.services.capture import clear_orphan_temp
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from bot.services.registry import missing_classes
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
    scraping,
    service,
    spamblock,
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


async def mark_stopped():
    """Bot shutdown (a clean stop): the next start is not after a crash."""
    try:
        os.remove(RUNNING_MARKER)
    except OSError:
        pass


async def run_bot():
    load_env()
    config = BotConfig()
    config.validate()

    clear_orphan_temp()  # sweep broadcast temp dirs a previous run was killed before cleaning
    shutil.rmtree(profile.PHOTO_TMP_DIR, ignore_errors=True)  # ...and the photo a profile job left

    settings = Settings()
    storage = SessionsStorage("sessions", settings.api_id, settings.api_hash, initialize=False)
    functions_storage = FunctionsStorage("functions", storage, settings)

    # reuse the CLI's auto-discovery: map class name -> ready function instance
    functions = {type(instance).__name__: instance for instance, _doc in functions_storage.functions}

    # fail fast with a clear message if the registry references a class that
    # auto-discovery didn't find (e.g. a renamed class), instead of a KeyError
    # deep inside a handler at first use.
    absent = missing_classes(functions)
    if absent:
        raise RuntimeError(
            "bot/services/registry.py lists function classes not found in functions/: "
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
    dp["personal"] = personal_storage(settings.api_id, settings.api_hash)
    dp["admins"] = config.admins  # who autoreply notifies
    dp.startup.register(announce_start)  # first: before "continuing the scrape"
    dp.shutdown.register(mark_stopped)
    dp.startup.register(scraping.resume_after_restart)
    dp.shutdown.register(scraping.stop_for_shutdown)
    dp.startup.register(autoreply.start)
    dp.shutdown.register(autoreply.stop)

    dp.update.outer_middleware(AuthMiddleware(config.admins))
    dp.message.outer_middleware(AlbumMiddleware())

    dp.include_router(menu.router)
    dp.include_router(accounts.router)
    dp.include_router(login.router)
    dp.include_router(autoreply_menu.router)
    dp.include_router(spamblock.router)
    dp.include_router(pm_broadcast.router)
    dp.include_router(invite.router)
    dp.include_router(join.router)
    dp.include_router(profile.router)
    dp.include_router(audience.router)
    dp.include_router(activity.router)
    dp.include_router(moderation.router)
    dp.include_router(service.router)
    dp.include_router(broadcasts.router)
    dp.include_router(scraping.router)

    await wait_for_bot_api(bot)  # before start_polling: no startup handler runs offline
    await dp.start_polling(bot)
