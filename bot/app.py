from aiogram import Bot, Dispatcher

from modules.settings import Settings
from modules.storages.functions_storage import FunctionsStorage
from modules.storages.sessions_storage import SessionsStorage

from bot.config import BotConfig
from bot.middlewares.auth import AuthMiddleware
from bot.services.album import AlbumMiddleware
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
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
    config = BotConfig()
    config.validate()

    settings = Settings()
    storage = SessionsStorage("sessions", settings.api_id, settings.api_hash, initialize=False)
    functions_storage = FunctionsStorage("functions", storage, settings)

    # reuse the CLI's auto-discovery: map class name -> ready function instance
    functions = {type(instance).__name__: instance for instance, _doc in functions_storage.functions}

    pool = WorkerPool(storage)

    bot = Bot(config.token)
    dp = Dispatcher()

    dp["pool"] = pool
    dp["functions"] = functions
    dp["settings"] = settings
    dp["manager"] = JobManager()

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
