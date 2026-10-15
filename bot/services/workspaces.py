"""Each project's own workers and job slots in the bot (modules/projects.py): one Workspace a project,
so the projects' jobs run side by side. A chat works in its current project — the one whose card it
opened last —: ProjectMiddleware gives that project's pool, slots and functions to the handlers."""
import functools
from dataclasses import dataclass, field

from aiogram import BaseMiddleware
from aiogram.dispatcher.flags import get_flag
from aiogram.types import CallbackQuery

from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from modules import projects
from modules.registry import PROJECTS

NO_PROJECT = f"⛔ Сначала откройте проект: {PROJECTS} → проект."


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


def chat_of(event) -> int:
    message = event.message if isinstance(event, CallbackQuery) else event
    return message.chat.id if message is not None else event.from_user.id


class ProjectMiddleware(BaseMiddleware):
    """Inner (it runs for a handler that matched): the chat's current project's pool, slots and
    functions for a project's handler (Workspace.deps). Without a current project — none opened since
    the bot started, or it was deleted — the handler doesn't run and the admin is sent to 📁 Проекты;
    unless `required` is off (the main menu's router: most of it works in no project), or the handler
    is flagged "any_project" (it finds the project it acts on by itself)."""

    def __init__(self, workspaces: Workspaces, required: bool = True):
        self.workspaces, self.required = workspaces, required

    async def __call__(self, handler, event, data):
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
