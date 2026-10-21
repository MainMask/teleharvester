import asyncio
import logging
import os
import shutil

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramNetworkError, TelegramServerError
from aiogram.types import BotCommand

from modules import instance_lock, projects as project_list
from modules.config import load_env
from modules.settings import Settings
from modules.storages.functions_storage import discover
from modules.scraper_creds import PERSONAL_DIR, personal_storage

from bot.config import BotConfig
from bot.middlewares.auth import AuthMiddleware
from bot.services import autoreply, watchdog
from bot.services.album import AlbumMiddleware
from bot.services.capture import clear_orphan_temp
from bot.services.safe_edit import SafeEdit
from bot.services.workspaces import ProjectMiddleware, Workspaces
from bot.callbacks import FunctionCB, MenuAction
from modules.registry import BOT_FUNCTIONS_BY_KEY, SCREENS, missing_classes
from bot.routers import (
    accounts,
    activity,
    autoreply as autoreply_menu,
    audience,
    chat_broadcasts,
    invite,
    join,
    login,
    mailing,
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
    mailing,
    chat_broadcasts,
    scraping,
    stale,  # last: a button nothing above took
)
# the work in a project (bot/services/workspaces.py): its handlers run on the chat's current project's
# workers — every router's but these, so a new one is a project's by default; of 📁 Проекты's, its card's
OUTSIDE_PROJECTS = (menu, projects, settings_menu, autoreply_menu, stale)
IN_PROJECT = (*(module for module in ROUTERS if module not in OUTSIDE_PROJECTS), projects.card, projects.manage)


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


async def announce_start(bot, admins, workspaces):
    """Bot startup: tell the admins it (re)started — after a crash, say so: systemd restarts it
    silently, and a crash loop would otherwise show only in journald."""
    crashed = os.path.exists(RUNNING_MARKER)
    os.makedirs(os.path.dirname(RUNNING_MARKER), exist_ok=True)
    open(RUNNING_MARKER, "w").close()

    status = ("⚠️ Бот перезапущен после сбоя — подробности: journalctl -u teleharvester-bot"
              if crashed else "🔄 Бот запущен")
    for admin in admins:
        try:
            await bot.send_message(admin, f"{status} · проектов: {len(workspaces.all())} · "
                                          f"воркеров: {workspaces.workers()}")
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


def unhandled_actions() -> list[str]:
    """The registry's buttons with no handler: a screen's action not in bot/callbacks.MenuAction, a
    function's key no FunctionCB filter takes, a project card's screens' button not in projects.card.ACTIONS."""
    actions = {button.action for screen in SCREENS.values() for button in screen.buttons}
    taken = {key for module in ROUTERS for key in function_keys(module.router)}
    card = {action.value for action in projects.card.CARD_ACTIONS} | projects.card.CARD_FUNCTIONS
    return sorted(actions - {action.value for action in MenuAction} | BOT_FUNCTIONS_BY_KEY.keys() - taken
                  | card - projects.card.ACTIONS.keys())


def function_keys(router) -> set[str]:
    """The function keys the router's (and its sub-routers') FunctionCB filters take."""
    rules = [flt.callback.rule for handler in router.callback_query.handlers for flt in handler.filters or ()
             if getattr(flt.callback, "callback_data", None) is FunctionCB]
    keys = {key for key in BOT_FUNCTIONS_BY_KEY if any(rule.resolve(FunctionCB(key=key)) for rule in rules)}
    for sub in router.sub_routers:
        keys |= function_keys(sub)
    return keys


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
    project_list.migrate()  # before anything reads a project: each needs its id (its folder)
    # reuse the CLI's auto-discovery: every project runs these classes on its own workers
    classes = discover("functions")

    # fail fast with a clear message if the registry references a class that
    # auto-discovery didn't find (e.g. a renamed class), instead of a KeyError
    # deep inside a handler at first use.
    absent = missing_classes(classes)
    if absent:
        raise RuntimeError(
            "modules/registry.py lists function classes not found in functions/: "
            + ", ".join(absent)
            + ". Rename them back or update BOT_FUNCTIONS."
        )
    # ...and every button's action a handler: not a ValueError the first time its screen is opened
    if unhandled := unhandled_actions():
        raise RuntimeError("modules/registry.py has buttons no handler takes: " + ", ".join(unhandled))

    workspaces = Workspaces(settings, classes)
    for project in project_list.load():
        workspaces.add(project)

    bot = Bot(config.token)
    bot.session.middleware(SafeEdit())  # an edit Telegram refuses: nothing, or the screen as a new message
    dp = Dispatcher()

    dp["workspaces"] = workspaces  # each project's pool and job slots
    dp["settings"] = settings
    os.makedirs(PERSONAL_DIR, exist_ok=True)  # a personal account added by phone lands there
    # personal accounts are scraper-only, never workers, and every project's
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
    in_project = ProjectMiddleware(workspaces)
    for module in IN_PROJECT:
        module.router.message.middleware(in_project)
        module.router.callback_query.middleware(in_project)
    # the screens (a project's: its own, by its buttons' project) and /cancel: the current project's, if any
    menu.router.message.middleware(ProjectMiddleware(workspaces, required=False))
    menu.router.callback_query.middleware(ProjectMiddleware(workspaces, required=False))

    await wait_for_bot_api(bot)  # before start_polling: no startup handler runs offline
    await dp.start_polling(bot)
