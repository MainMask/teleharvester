"""A project's card's screens on its files (registry.PROJECT_FILES: 📥 Сбор, 📨 База и рассылка, ⚙️ Управление):
their buttons and functions, on the project's files and workers, its own settings (manage.py)."""
import asyncio
from dataclasses import dataclass

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import FunctionCB, MenuAction, MenuCB
from bot.routers import audience, mailing, scraping
from bot.routers._common import NO_WORKERS
from bot.routers.projects import manage, views
from bot.services import screens
from bot.services.workspaces import Workspace, Workspaces
from modules import projects, scraped_files
from modules.registry import BOT_FUNCTIONS, COLLECT, MANAGE, PROJECT_FILES, SCREENS

router = Router()


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


async def _base(tap: Tap, kinds: tuple[str, ...], ask):
    """The mailing or the contacts, on the project's bases only: the base is still picked by button."""
    await ask(tap.message, tap.state, scraped_files.project_files(tap.project.name, kinds=kinds))


# every button of the card's screens on the project's files, by its action or function key (checked at the
# bot's start: bot/app.py unhandled_actions)
ACTIONS = {
    # 📥 Сбор
    "scrape": lambda tap: scraping.scrape_project(tap.message, tap.state, tap.project, tap.scraper),
    "topup": _topup,
    "upload": lambda tap: manage.ask_upload(tap.message, tap.state, tap.project),
    "members": lambda tap: scraping.members_project(tap.message, tap.state, tap.project, tap.scraper),
    "verify": lambda tap: scraping.verify_project(tap.message, tap.state, tap.project, tap.scraper),
    "analysis": lambda tap: scraping.analysis_project(tap.message, tap.state, tap.project),
    # 📨 База и рассылка
    "addcontacts": lambda tap: _base(tap, ("participants",), audience.ask_base),
    "pmmailing": lambda tap: _base(tap, ("participants", "list"), mailing.ask_recipients),
    # ⚙️ Управление
    "channels": lambda tap: manage.ask_channels(tap.message, tap.state, tap.project),
    "rename": lambda tap: manage.ask_rename(tap.message, tap.state, tap.project),
    "clearbase": lambda tap: manage.offer_clear(tap.message, tap.state, tap.project),
    "delete": lambda tap: manage.confirm_delete(tap.message, tap.state, tap.project),
}
CARD_ACTIONS = {MenuAction(button.action) for title in PROJECT_FILES for button in SCREENS[title].buttons}
CARD_FUNCTIONS = {function.key for function in BOT_FUNCTIONS if function.category in PROJECT_FILES}
ON_WORKERS = {"addcontacts", "pmmailing"}  # the card's functions run by the workers, not on one account


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
    """📥 Сбор's and ⚙️ Управление's view: what of them the project's files make (views.show_screen)."""
    async def view(message: Message, state: FSMContext, edit: bool, workspaces: Workspaces, **_):
        project = await views.current(message, state, workspaces)
        if project is not None:
            await views.show_screen(message, project, title, edit)
    return view


for _title in (COLLECT, MANAGE):
    screens.view(_title)(_files_screen(_title))
