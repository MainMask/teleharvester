from aiogram import Bot, Dispatcher

from modules.config import load_env
from modules.settings import Settings
from modules.storages.functions_storage import FunctionsStorage
from modules.scraper_creds import personal_storage
from modules.storages.sessions_storage import SessionsStorage

from bot.config import BotConfig
from bot.middlewares.auth import AuthMiddleware
from bot.services.album import AlbumMiddleware
from bot.services.capture import clear_orphan_temp
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from bot.services.registry import missing_classes
from bot.routers import (
    accounts,
    activity,
    audience,
    broadcasts,
    invite,
    join,
    menu,
    moderation,
    pm_broadcast,
    profile,
    scraping,
    service,
    spamblock,
)


async def run_bot():
    load_env()
    config = BotConfig()
    config.validate()

    clear_orphan_temp()  # sweep broadcast temp dirs a previous run was killed before cleaning

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
    dp.startup.register(scraping.resume_after_restart)
    dp.shutdown.register(scraping.stop_for_shutdown)

    dp.update.outer_middleware(AuthMiddleware(config.admins))
    dp.message.outer_middleware(AlbumMiddleware())

    dp.include_router(menu.router)
    dp.include_router(accounts.router)
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

    await dp.start_polling(bot)
