import html

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import ChoiceCB, NavCB
from bot.keyboards.common import choice_kb, scrape_kb, stop_kb
from bot.keyboards.menu import back_kb, main_menu
from bot.routers._common import STALE_BUTTON, leave_form
from bot.services import screens
from bot.services.jobs import JobManager
from bot.services.workspaces import NO_PROJECT, Workspace, Workspaces
from bot.states import origin
from modules import projects
from modules.registry import MAIN_MENU, PROJECTS, SCREENS, TASKS, in_project

router = Router()


def welcome(name: str, projects_count: int, workers: int) -> str:
    """The /start message: each main-menu button with what it holds (registry SCREENS hints), the commands."""
    lines = []
    for label in MAIN_MENU:
        icon, title = label.split(" ", 1)
        lines.append(f"{icon} <b>{title}</b> — {SCREENS[label].hint}")
    sections = "\n".join(lines)
    return (f"👋 Здравствуйте, <b>{html.escape(name)}</b>!\n\n"
            "Добро пожаловать в <b>teleharvester</b> — вашу панель управления Telegram-аккаунтами.\n\n"
            f"📁 Проектов: <b>{projects_count}</b> · 🤖 воркеров: <b>{workers}</b>\n\n{sections}\n\n"
            "/status — что сейчас выполняется · /cancel — выйти из ввода или остановить задачу\n\n"
            "<i>Выберите раздел в меню ниже 👇</i>")


@router.message(CommandStart())
async def start(message: Message, state: FSMContext, workspaces: Workspaces):
    await leave_form(message, state)
    await message.answer(
        welcome(message.from_user.first_name, len(workspaces.all()), workspaces.workers()),
        parse_mode="HTML",
        reply_markup=main_menu(),
    )


async def _leave_form(message: Message, state: FSMContext, manager: JobManager | None, workspace: Workspace | None,
                      edit: bool = False):
    """Out of the form, back to the screen it was started from (states.origin): ✖️'s in its prompt's place."""
    screen = origin(await state.get_state(), await state.get_data())
    # backing out of a form must not stop a job that may belong to another admin
    await leave_form(message, state)
    note = f"\nЗадача «{manager.label}» продолжает работу — /cancel ещё раз, чтобы остановить." \
        if manager is not None and manager.active else ""
    if screen is None:  # the main menu's keyboard: a new message only
        await message.answer("Отменено." + note, reply_markup=main_menu())
        return
    project = workspace.project_id if workspace is not None and in_project(screen) else 0
    await screens.send(message, edit)("Отменено." + note, reply_markup=back_kb(screen, project))


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext, manager: JobManager | None = None,
                 workspace: Workspace | None = None):
    """A form: left. Else the current project's job (manager: none without a current project) stops."""
    if await state.get_state() is not None:
        await _leave_form(message, state, manager, workspace)
        return

    # abort the active job if one is open (a task or an interactive flow)
    stopped = manager is not None and await manager.stop()
    if not stopped and manager is not None and manager.active:  # a report already replaying, a tdata import
        await message.answer(
            f"Задача «{manager.label}» не прерывается, дождитесь завершения.", reply_markup=main_menu()
        )
        return
    await message.answer("Отменено." if stopped else "Нечего отменять.", reply_markup=main_menu())


@screens.view(TASKS)  # 📊 Задачи: the same
async def tasks(message: Message, state: FSMContext, edit: bool, workspaces: Workspaces, **_):
    await status(message, workspaces)


@router.message(Command("status"))
async def status(message: Message, workspaces: Workspaces):
    """What runs now in every project, each job with its 📊 and ⏹ as under its start message (which
    may be far up); a form under way stays where it is."""
    try:
        names = {project.id: project.name for project in projects.load()}
    except ValueError:  # the projects' file can't be read: the jobs are shown all the same
        names = {}
    active = []
    for workspace in workspaces.all():
        for slot, markup in ((workspace.manager, stop_kb), (workspace.scrapes, scrape_kb), (workspace.analyses, None)):
            if slot.active:
                active.append((names.get(workspace.project_id, "?"), slot, markup and markup(slot.key)))
    if not active:
        await message.answer(f"{TASKS} — сейчас ничего не выполняется.")
        return
    for index, (name, slot, markup) in enumerate(active):
        head = "" if index else f"{TASKS} — выполняется: {len(active)}\n\n"  # on the first job's message
        label = f"📁 {name} · {slot.label}"
        if slot.progress is None:  # a dialog holding the slot (its ⏹ if it can be left), or a job with nothing to count
            stop = choice_kb("job_stop", [("⏹ Стоп", str(slot.key))]) if slot.stoppable else None
            await message.answer(f"{head}▶️ {label}", reply_markup=stop)
        else:
            await message.answer(head + slot.progress.render(label), reply_markup=markup)


@router.callback_query(ChoiceCB.filter(F.scope == "cancel"))
async def cancel_button(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext,
                        manager: JobManager | None = None, workspace: Workspace | None = None):
    """✖️ Отмена under a prompt (keyboards.common.cancel_kb): /cancel's form branch, only at its own step."""
    if await state.get_state() != callback_data.value.replace(".", ":"):
        await callback.answer(STALE_BUTTON)
        return
    await callback.answer()
    await _leave_form(callback.message, state, manager, workspace, edit=True)


@router.callback_query(ChoiceCB.filter(F.scope.in_({"job_stop", "scrape_stop"})))
async def stop_job(callback: CallbackQuery, callback_data: ChoiceCB, workspaces: Workspaces):
    """⏹ under a job, whichever project's (/status lists them all): its slot's job stops. A scrape's, a members
    list's or a verify's ("scrape_stop", see scraping.common.take_slot) keeps what it collected; here, not in
    the project's routers: a scrape continued after a restart runs in no chat's current project."""
    slot = workspaces.slot(int(callback_data.value)) if callback_data.value.isdigit() else None
    scraper = callback_data.scope == "scrape_stop"
    if slot is not None and await slot.stop():
        await callback.answer("Останавливаю… собранное сохранится." if scraper else "Останавливаю…")
    else:
        await callback.answer("Нечего останавливать." if scraper else
                              "Нечего останавливать (или задача не прерывается).")


@router.callback_query(ChoiceCB.filter(F.scope == "job_progress"))
async def show_progress(callback: CallbackQuery, callback_data: ChoiceCB, workspaces: Workspaces):
    slot = workspaces.slot(int(callback_data.value)) if callback_data.value.isdigit() else None
    if slot is None or slot.progress is None:
        await callback.answer("Нет активной задачи.")
        return
    await callback.answer(slot.progress.render(slot.label), show_alert=True)


# Every screen, opened the same way (bot/services/screens.py): a main-menu button — from any form's
# step too, this router being the first (bot/app.py) — as a new message; a screen's button
# (›, ⬅️ Назад) in that message's place instead of piling up messages.

async def _open(title: str, message: Message, state: FSMContext, edit: bool, deps: dict):
    if in_project(title) and "workspace" not in deps:  # no current project (ProjectMiddleware)
        await message.answer(NO_PROJECT, reply_markup=back_kb(PROJECTS))
        return
    if not SCREENS[title].keeps_form:
        await leave_form(message, state)
    await screens.open_screen(title, message, state, edit, deps)


@router.message(F.text.in_(MAIN_MENU))
async def open_from_menu(message: Message, state: FSMContext, **deps):
    await _open(message.text, message, state, False, deps)


@router.callback_query(NavCB.filter(F.screen.in_(SCREENS)))
async def open_by_button(callback: CallbackQuery, callback_data: NavCB, state: FSMContext, **deps):
    """A screen's button (its project's, if it is one: ProjectMiddleware made it the current one)."""
    if callback_data.fresh:  # a report's ⬅️: the report stays, without it (an old one's: SafeEdit)
        await callback.message.edit_reply_markup(reply_markup=None)
    await _open(callback_data.screen, callback.message, state, not callback_data.fresh, deps)
    await callback.answer()
