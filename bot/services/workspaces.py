"""Each project's own workers and job slots in the bot (modules/projects.py): one Workspace a project,
so the projects' jobs run side by side. A chat works in its current project — the one whose card or
screen's button it tapped last —: ProjectMiddleware gives that project's pool, slots and functions to the
handlers."""
import functools
import os
from dataclasses import dataclass, field

from aiogram import BaseMiddleware
from aiogram.dispatcher.flags import get_flag
from aiogram.types import CallbackQuery

from bot.services import login as logins
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from modules import projects
from modules.registry import PROJECTS
from modules.scraper_creds import find_account, scrape_accounts
from modules.storages.sessions_storage import release_client

NO_PROJECT = f"⛔ Сначала откройте проект: {PROJECTS} → проект."
PROJECT_GONE = f"Проекта уже нет — откройте {PROJECTS}."


@dataclass
class Workspace:
    project_id: int
    pool: WorkerPool
    functions: dict  # class name -> a new instance on the project's workers (see Workspaces.add)
    manager: JobManager = field(default_factory=JobManager)  # the workers' jobs
    # the scraper's jobs (scrape, members, verify) run on their own client in their own slot, so a
    # multi-day scrape doesn't hold up the other tasks
    scrapes: JobManager = field(default_factory=JobManager)
    # the analysis reads files in a child process, no account: a mailing or a scrape doesn't hold it up
    analyses: JobManager = field(default_factory=JobManager)

    def __post_init__(self):
        for slot in self.slots:  # a job's ⬅️ leads back into its project
            slot.project_id = self.project_id

    @property
    def slots(self) -> tuple[JobManager, ...]:
        return self.manager, self.scrapes, self.analyses

    def deps(self) -> dict:
        """What a project's handlers take, by name."""
        return dict(workspace=self, pool=self.pool, functions=self.functions, manager=self.manager,
                    scrapes=self.scrapes, analyses=self.analyses)


class Workspaces:
    def __init__(self, settings, classes: dict):
        self.settings = settings
        self.classes = classes  # every *Func, by name (functions_storage.discover)
        self._by_id: dict[int, Workspace] = {}
        self.current: dict[int, int] = {}  # chat id -> the project id its card was opened last
        self._pools: list[WorkerPool] = []  # every project's: WorkerPool.peers

    def add(self, project: projects.Project) -> Workspace:
        storage = projects.storage(project, self.settings.api_id, self.settings.api_hash)
        pool = WorkerPool(storage)
        pool.peers = self._pools
        self._pools.append(pool)
        # a new instance for each run (resolve), so no run inherits another's state
        functions = {name: functools.partial(cls, storage, self.settings) for name, cls in self.classes.items()}
        workspace = self._by_id[project.id] = Workspace(project.id, pool, functions)
        return workspace

    def remove(self, project_id: int):
        """A project deleted: its workspace goes (it has no workers and no job: projects.remove)."""
        workspace = self._by_id.pop(project_id, None)
        if workspace is not None:
            self._pools.remove(workspace.pool)
        self.current = {chat: current for chat, current in self.current.items() if current != project_id}

    def all(self) -> list[Workspace]:
        return list(self._by_id.values())

    def get(self, project_id: int) -> Workspace | None:
        return self._by_id.get(project_id)

    def open(self, chat_id: int, project: projects.Project) -> Workspace:
        """The chat works in this project from now on."""
        workspace = self.get(project.id) or self.add(project)
        self.current[chat_id] = project.id
        return workspace

    def of_chat(self, chat_id: int) -> Workspace | None:
        project_id = self.current.get(chat_id)
        return None if project_id is None else self.get(project_id)

    def slot(self, key: int) -> JobManager | None:
        """The job slot a 📊 / ⏹ button names (JobManager.key), whichever project's."""
        return next((slot for workspace in self.all() for slot in workspace.slots if slot.key == key), None)

    def workers(self) -> int:
        return sum(workspace.pool.count() for workspace in self.all())

    def username(self, path: str) -> str | None:
        """A worker's known @username, whichever project's."""
        return next((name for workspace in self.all() if (name := workspace.pool.storage.usernames.get(path))),
                    None)


def take_free(pool: WorkerPool, paths: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """Free accounts (projects.FREE_DIR) into the project's pool: the labels of the ones moved, and
    (label, why) of the rest."""
    moved, failed = [], []
    for path in paths:
        if not os.path.exists(path):  # taken meanwhile, from another chat
            failed.append((os.path.basename(path), "его уже нет среди свободных"))
            continue
        label = projects.account_label(path)
        try:
            pool.storage.add_jsession(projects.move_account(path, pool.storage.directory))
        except (ValueError, OSError) as err:
            failed.append((label, str(err)))
            continue
        moved.append(label)
    return moved, failed


async def move_out(pool: WorkerPool, paths: list[str], project: projects.Project | None,
                   target: Workspace | None) -> tuple[list[str], list[tuple[str, str]]]:
    """The project's workers to another `project` (`target`: its workspace, if it was opened since the start;
    else it reads its folder when opened) or, with no project, to the free ones: as take_free."""
    moved, failed = [], []
    for path in paths:
        account = find_account(scrape_accounts(pool.storage), path)
        if account is None:  # moved or gone meanwhile
            failed.append((os.path.basename(path), "его уже нет в проекте"))
            continue
        # one account, one job: not one a job, the auto-reply, a code or a scrape uses now
        if pool.busy(path) or pool.scraped(path):
            failed.append((account.label, "занят задачей — перенесите после её окончания"))
            continue
        try:  # no await from the check to here: nothing takes it meanwhile
            new = projects.move_account(path, projects.FREE_DIR if project is None else projects.sessions_dir(project))
        except (ValueError, OSError) as err:
            failed.append((account.label, str(err)))
            continue
        pool.storage.forget_session(path)
        await release_client(account.client)
        if target is not None:
            target.pool.storage.add_jsession(new)
        moved.append(account.label)
    return moved, failed


def chat_of(event) -> int:
    message = event.message if isinstance(event, CallbackQuery) else event
    return message.chat.id if message is not None else event.from_user.id


class ProjectMiddleware(BaseMiddleware):
    """Inner (it runs for a handler that matched, its callback data parsed): the chat's current project's
    pool, slots and functions for a project's handler (Workspace.deps). A project's button (its callback
    data's `project`, bot/callbacks.py) makes its project the current one first; a form under way in
    another project is left then — its steps would act on this one. Without a current project — none
    opened since the bot started, or it was deleted — the handler doesn't run and the admin is sent to
    📁 Проекты; unless `required` is off (the main menu's router: most of it works in no project), or the
    handler is flagged "any_project" (it finds the project it acts on by itself)."""

    def __init__(self, workspaces: Workspaces, required: bool = True):
        self.workspaces, self.required = workspaces, required

    async def _enter(self, event: CallbackQuery, data, project_id: int) -> bool:
        """The chat into the button's project; False after saying it is gone."""
        chat = chat_of(event)
        if self.workspaces.current.get(chat) == project_id:
            return True
        try:
            project = next((p for p in projects.load() if p.id == project_id), None)
        except ValueError:  # an unreadable projects file: 📁 Проекты says why
            project = None
        if project is None:
            await event.answer(PROJECT_GONE, show_alert=True)
            return False
        await logins.close(chat)
        await data["state"].clear()
        self.workspaces.open(chat, project)
        return True

    async def __call__(self, handler, event, data):
        project_id = getattr(data.get("callback_data"), "project", 0)
        if project_id and not await self._enter(event, data, project_id):
            return None
        workspace = self.workspaces.of_chat(chat_of(event))
        if workspace is not None:
            data.update(workspace.deps())
        elif self.required and not get_flag(data, "any_project"):
            if isinstance(event, CallbackQuery):
                await event.answer(NO_PROJECT, show_alert=True)
            else:
                await event.answer(NO_PROJECT)
            return None
        return await handler(event, data)
