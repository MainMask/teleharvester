"""What the projects' handlers share: the list, a project's card and its screens on its files as shown, the
channels' prompt.

A project's card makes it the chat's current project (bot/services/workspaces.py); its buttons and its
screens' carry its id, so each acts on its own project, whichever the chat is in by then."""
import asyncio
import html

from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from bot.keyboards.common import cancel_kb, choice_kb, paged_kb
from bot.keyboards.menu import back_kb, screen_kb
from bot.routers._common import CHAT_FORMAT, menu_answer
from bot.services import screens
from bot.services.workspaces import PROJECT_GONE, Workspaces
from bot.states import Projects
from modules import projects, scraped_files, warmup
from modules.registry import ADD_CHANNELS, BASE, CARD, COLLECT, MANAGE, PROJECTS, SCREENS

STALE_LIST = f"Кнопка устарела — откройте {PROJECTS} заново."
CHANNELS_ASK = (f"Пришлите каналы или группы через запятую ({CHAT_FORMAT}).\n"
                "Их названия проверятся при скрапе, на выбранном для него аккаунте.")
NEW_PROJECT = "➕ Новый проект"
FROM_SCRAPE = "📥 Из готового скрапа"  # a project made of a finished scrape's files
# a project without channels: by its button under the channels' prompt, never typed
NO_CHANNELS = {"new": (Projects.channels, "📭 Без каналов"), "edit": (Projects.edit, "📭 Убрать все каналы")}


def channels_kb(step: str):
    """The channels' prompt's buttons: the one for a project without them, ✖️ Отмена."""
    state, label = NO_CHANNELS[step]
    return cancel_kb(state, choice_kb("prj_none", [(label, step)]))


async def load(message: Message, state: FSMContext) -> list | None:
    """The projects; None after telling why (an unreadable file) and back to the menu."""
    try:
        return projects.load()
    except ValueError as err:
        await state.clear()
        await menu_answer(message, f"⚠️ {err}")
        return None


async def find(callback: CallbackQuery, state: FSMContext, name: str | None) -> projects.Project | None:
    """The project a button names (a list's, a confirmation's); None after telling why — a stale button (or a
    project gone meanwhile) in a pop-up, an unreadable file in a message —, the tap answered then."""
    items = await load(callback.message, state) if name else []
    project = next((p for p in items if p.name == name), None) if items is not None else None
    if project is None:
        await callback.answer(STALE_LIST if items is not None else None, show_alert=items is not None)
    return project


async def current(message: Message, state: FSMContext, workspaces: Workspaces) -> projects.Project | None:
    """The chat's current project (the one its last button acted on); None after telling why (gone meanwhile)."""
    workspace = workspaces.of_chat(message.chat.id)
    items = await load(message, state) if workspace is not None else []
    if items is None:
        return None
    project = next((p for p in items if p.id == workspace.project_id), None)
    if project is None:
        await message.answer(PROJECT_GONE, reply_markup=back_kb(PROJECTS))
    return project


async def show_list(message: Message, state: FSMContext, note: str = "", edit: bool = False, page: int = 0):
    """The projects' list, a `page` of them (keyboards.paged_kb); `edit`: in place of the message (a
    button's), not a new one."""
    items = await load(message, state)
    if items is None:
        return
    await state.clear()
    await state.update_data(project_names=[p.name for p in items])  # buttons carry an index into this
    if items:
        text = (f"{screens.title_line(PROJECTS)} — у каждого свои аккаунты, прокси и база. В карточке проекта — "
                f"{html.escape(SCREENS[CARD].hint)}. ⏸ — есть незавершённый скрап.")
    else:
        text = (f"{screens.title_line(PROJECTS)} — пока ни одного. У проекта свои аккаунты и прокси и своя "
                "аудитория: каналы для скрапа, участники групп или список. Готовые файлы скрапа станут проектом через "
                f"«{FROM_SCRAPE}».")
    listed = paged_kb("prj", [(projects.label(p), str(i)) for i, p in enumerate(items)], page)
    actions = [(NEW_PROJECT, "new")]
    if scraped_files.scrape_names(limit=1):  # which of them can be a project: asked on the button
        actions.append((FROM_SCRAPE, "import"))
    markup = InlineKeyboardMarkup(inline_keyboard=[*listed.inline_keyboard,
                                                   *choice_kb("prj", actions).inline_keyboard])
    await screens.send(message, edit)(f"{html.escape(note)}\n\n{text}" if note else text, parse_mode="HTML",
                                      reply_markup=markup)


async def show_card(message: Message, state: FSMContext, workspaces: Workspaces, project: projects.Project,
                    edit: bool = False, note: str = ""):
    """The project's card, the chat's current project from now on; `edit`: in place of the message (a
    button's), not a new one; `note` above it."""
    names = (await state.get_data()).get("project_names")
    await state.clear()
    # the list shown before keeps working: its buttons carry an index into project_names
    await state.update_data(project_names=names or [])
    workspace = workspaces.open(message.chat.id, project)
    # reads the files' footers (and the posts file's attrs), the warmup's state: off the event loop
    text = await asyncio.to_thread(lambda: projects.summary(project, workspace.pool.count(), True,
                                                            warmup.summary(workspace.pool.storage)))
    await screens.send(message, edit)(f"{html.escape(note)}\n\n{text}" if note else text, parse_mode="HTML",
                                      reply_markup=screen_kb(CARD, project.id))


def _files_view(project: projects.Project, title: str) -> tuple[str, dict]:
    """A card's screen on the project's files: its own text, and what of its buttons it shows (screens.show's
    `hidden` and `titles`); reads the files, so off the event loop."""
    if title == COLLECT:  # a top-up while a scrape is unfinished would drop its checkpoint: ▶️ continues that
        topup = projects.topup(project) is not None and projects.unfinished(project.name) is None
        # 📢 Каналы named ➕ Добавить каналы while the project has none
        titles = {} if project.channels else {"channels": ADD_CHANNELS}
        return (projects.collect_text(project, html=True, budget=2500),
                dict(hidden=() if topup else ("topup",), titles=titles))
    if title == BASE:  # 🧹 only for what there is to clear: a group members' base or a list (only added to)
        clear = any(path(project.name).exists() for path in projects.CLEARABLE.values())
        return projects.base_text(project, html=True), dict(hidden=() if clear else ("clearbase",))
    if title == MANAGE:  # 🌍 with the project's region on it: its button cycles it
        region = warmup.region_title(projects.home(project))
        return "", dict(titles={"region": region})
    return "", {}


async def show_done(message: Message, state: FSMContext, project: projects.Project, title: str, note: str,
                    edit: bool = False):
    """A flow on the project's files is over: the screen it was started from, `note` above it; the projects'
    list shown before keeps working (show_card's)."""
    names = (await state.get_data()).get("project_names")
    await state.clear()
    await state.update_data(project_names=names or [])
    await show_screen(message, project, title, edit, note)


async def show_screen(message: Message, project: projects.Project, title: str, edit: bool = True,
                      note: str = ""):
    """A card's screen on the project's files (registry.PROJECT_FILES), `note` above it."""
    own, shown = await asyncio.to_thread(_files_view, project, title)
    header = "\n\n".join(block for block in (html.escape(note), screens.title_line(title, project.name), own)
                          if block)
    await screens.show(message, title, edit, header, project.id, **shown)
