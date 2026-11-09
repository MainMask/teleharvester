"""A project's card's screens on its files (registry.PROJECT_FILES: 📥 Сбор, 🎯 База и рассылка, 🛠 Управление):
their buttons and functions, on the project's files and workers, its own settings (manage.py)."""
import asyncio
import os
import tempfile
from dataclasses import dataclass

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, FSInputFile, Message

from bot.callbacks import FunctionCB, MenuAction, MenuCB
from bot.routers import audience, mailing, scraping
from bot.routers.projects import manage, views
from bot.services import screens
from bot.services.workspaces import Workspace, Workspaces
from modules import project_base, projects, scraped_files, warmup
from modules.registry import BOT_FUNCTIONS, MANAGE, NO_WORKERS, ON_WORKERS, PROJECT_FILES, SCREENS
from modules.scraper_creds import scrape_accounts

router = Router()

ON_ACCOUNT = ("scrape", "topup", "members", "verify")  # the card's functions run on one account, a worker or personal


@dataclass
class Tap:
    """A card's screen's button pressed: on its project, the chat's current one (ProjectMiddleware)."""
    callback: CallbackQuery
    state: FSMContext
    project: projects.Project
    workspace: Workspace
    workspaces: Workspaces
    personal: object

    @property
    def message(self):
        return self.callback.message

    @property
    def scraper(self) -> dict:
        """What the scraper's flows take (bot/routers/scraping/)."""
        return dict(pool=self.workspace.pool, personal=self.personal, scrapes=self.workspace.scrapes)


async def _topup(tap: Tap):
    plan = await asyncio.to_thread(projects.topup, tap.project)  # reads the posts file's footer
    if plan is None:  # its posts file gone since projects.missing read it: the tap answered, so a message
        await scraping.common.to_collect(tap.message, projects.NOTHING_TO_TOPUP, tap.workspace.scrapes)
        return
    await scraping.topup_project(tap.message, tap.state, tap.project, plan, tap.scraper)


async def _region(tap: Tap):
    """🌍: the project's next region (its content's: 👤 Профиль's lists, the warmup), 🛠 Управление anew in place."""
    home = projects.home(tap.project)
    warmup.save_settings(home, warmup.cycle(warmup.load_settings(home), "region"))
    await views.show_screen(tap.message, tap.project, MANAGE)


async def _base(tap: Tap, kinds: tuple[str, ...], ask):
    """The mailing or the contacts, on the project's bases only, picked by button: 📚 Вся база first, with its people
    (each once; the contacts' only those with an id, not the list's lines), then each file."""
    name = tap.project.name
    whole = await asyncio.to_thread(project_base.people, name, "list" not in kinds)
    await ask(tap.message, tap.state, name, whole, scraped_files.project_files(name, kinds=kinds))


async def _export(tap: Tap):
    """📦 Скачать базу: 📚 Вся база as a .csv document under 🎯 (the screen stays, as an accounts' ZIP)."""
    name = tap.project.name
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, f"{name}_base.csv")
        count = await asyncio.to_thread(project_base.export, name, path)
        try:
            await tap.message.answer_document(FSInputFile(path),
                                              caption=f"📦 База проекта «{name}»: людей {count}, без повторов.")
        except TelegramAPIError as err:  # e.g. past the Bot API's 50 MB
            await tap.message.answer(f"⚠️ Не удалось отправить базу: {err}")


# every button of the card's screens on the project's files, by its action or function key (checked at the
# bot's start: bot/app.py unhandled_actions)
ACTIONS = {
    # 📥 Сбор
    "scrape": lambda tap: scraping.scrape_project(tap.message, tap.state, tap.project, tap.scraper),
    "topup": _topup,
    "members": lambda tap: scraping.members_project(tap.message, tap.state, tap.project, tap.scraper),
    "verify": lambda tap: scraping.verify_project(tap.message, tap.state, tap.project, tap.scraper),
    "analysis": lambda tap: scraping.analysis_project(tap.message, tap.state, tap.project),
    # 🎯 База и рассылка
    "upload": lambda tap: manage.ask_upload(tap.message, tap.state, tap.project),
    "clearbase": lambda tap: manage.offer_clear(tap.message, tap.state, tap.project),
    "addcontacts": lambda tap: _base(tap, ("participants",), audience.ask_base),
    "pmmailing": lambda tap: _base(tap, ("participants", "list"), mailing.ask_recipients),
    "exportbase": _export,
    # 🛠 Управление
    "channels": lambda tap: manage.ask_channels(tap.message, tap.state, tap.project),
    "region": _region,
    "rename": lambda tap: manage.ask_rename(tap.message, tap.state, tap.project),
    "delete": lambda tap: manage.confirm_delete(tap.message, tap.state, tap.project),
}
CARD_ACTIONS = {MenuAction(button.action) for title in PROJECT_FILES for button in SCREENS[title].buttons}
CARD_FUNCTIONS = {function.key for function in BOT_FUNCTIONS if function.category in PROJECT_FILES}


async def _press(callback: CallbackQuery, key: str, state: FSMContext, workspace: Workspace,
                 workspaces: Workspaces, personal):
    project = await views.current(callback.message, state, workspaces)
    if project is None:
        await callback.answer()
        return
    # a function with nothing to work on yet — what to collect first —, or no worker to run it: in a pop-up over
    # the screen
    why = await asyncio.to_thread(projects.missing, project, key) if key in projects.MISSING_CHECKED else None
    if not why and key in ON_WORKERS and workspace.pool.count() == 0:
        why = NO_WORKERS
    if not why and key in ON_ACCOUNT and not scrape_accounts(workspace.pool.storage, personal):
        why = scraping.common.NO_ACCOUNTS
    await callback.answer(why, show_alert=bool(why))
    if not why:
        await ACTIONS[key](Tap(callback, state, project, workspace, workspaces, personal))


@router.callback_query(MenuCB.filter(F.action.in_(CARD_ACTIONS)))
async def card_action(callback: CallbackQuery, callback_data: MenuCB, state: FSMContext, workspace: Workspace,
                      workspaces: Workspaces, personal):
    await _press(callback, callback_data.action.value, state, workspace, workspaces, personal)


@router.callback_query(FunctionCB.filter(F.key.in_(CARD_FUNCTIONS)))
async def card_function(callback: CallbackQuery, callback_data: FunctionCB, state: FSMContext,
                        workspace: Workspace, workspaces: Workspaces, personal):
    await _press(callback, callback_data.key, state, workspace, workspaces, personal)


def _files_screen(title: str):
    """The view of a card's screen on the project's files: what of it the files make (views.show_screen)."""
    async def view(message: Message, state: FSMContext, edit: bool, workspaces: Workspaces, **_):
        project = await views.current(message, state, workspaces)
        if project is not None:
            await views.show_screen(message, project, title, edit)
    return view


for _title in PROJECT_FILES:
    screens.view(_title)(_files_screen(_title))
