"""📁 Проекты: the list, a new project (its name, then its channels), one made from a finished scrape's files.

A project's scrape runs through the scrape flow (scraping.scrape_project) under the project's name, so its
files, and an interrupted scrape to continue, are found by that name."""
import asyncio

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import ChoiceCB
from bot.keyboards.common import cancel_kb, choice_kb, page_of
from bot.routers._common import back_answer, left_flow, menu_answer, pick_from, require_text, typed_channels
from bot.routers.projects import manage, views
from bot.services import screens
from bot.services.workspaces import Workspaces
from bot.states import Projects
from modules import projects, scraped_files
from modules.registry import CARD, MAIN_MENU, PROJECTS

router = Router()


@screens.view(PROJECTS)
async def projects_screen(message: Message, state: FSMContext, edit: bool, **_):
    await views.show_list(message, state, edit=edit)


@screens.view(CARD)  # ⬅️ from its screens: its project's card (ProjectMiddleware made it the current one)
async def card_screen(message: Message, state: FSMContext, edit: bool, workspaces: Workspaces, **_):
    project = await views.current(message, state, workspaces)
    if project is not None:
        await views.show_card(message, state, workspaces, project, edit=edit)


@router.callback_query(ChoiceCB.filter(F.scope == "prj"))
async def project_pick(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext, workspaces: Workspaces):
    if callback_data.value not in ("new", "import") and page_of(callback_data.value) is None:  # a project's
        name = pick_from((await state.get_data()).get("project_names", []), callback_data.value)
        if (project := await views.find(callback, state, name)) is not None:
            await callback.answer()
            await views.show_card(callback.message, state, workspaces, project, edit=True)
        return
    await callback.answer()
    if callback_data.value == "new":
        await state.set_state(Projects.name)
        await callback.message.answer("➕ Новый проект — имя проекта, оно же имя его файлов скрапа (например "
                                      "GreenBase). Готовые файлы с этим именем сразу окажутся в проекте:",
                                      reply_markup=cancel_kb(Projects.name))
        return
    if callback_data.value == "import":
        await _offer_import(callback.message, state)
        return
    # ◀️ · 📄 · ▶️: another page, the list read again (📄, the page shown: SafeEdit)
    await views.show_list(callback.message, state, edit=True, page=page_of(callback_data.value))


async def _offer_import(message: Message, state: FSMContext):
    try:  # reads the posts files' channels: off the event loop
        found = await asyncio.to_thread(projects.importable)
    except ValueError as err:  # an unreadable projects file
        await state.clear()
        await menu_answer(message, f"⚠️ {err}")
        return
    if not found:
        await message.edit_text("Не из чего сделать проект: у готовых скрапов уже есть проекты, "
                                "или в их файлах не записаны каналы (такой проект создайте через «➕ Новый проект»).",
                                reply_markup=choice_kb("prj_imp", [("⬅️ Назад", "back")]))
        return
    await state.update_data(import_items=found)  # buttons carry an index into this
    await message.edit_text(  # in the list's place, as a card
        f"Готовые скрапы из {scraped_files.BASES_DIR}: проект получит имя и каналы скрапа, его файлы — "
        "сразу в проекте.",
        reply_markup=choice_kb("prj_imp", [*[(projects.label(projects.Project(name, channels)), str(i))
                                             for i, (name, channels) in enumerate(found)],
                                           ("⬅️ Назад", "back")]))


@router.callback_query(ChoiceCB.filter(F.scope == "prj_imp"))
async def project_import(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext,
                         workspaces: Workspaces):
    item = pick_from((await state.get_data()).get("import_items", []), callback_data.value)
    if item is None and callback_data.value != "back":  # a stale button
        await callback.answer(views.STALE_LIST, show_alert=True)
        return
    await callback.answer()
    if callback_data.value == "back":
        await views.show_list(callback.message, state, edit=True)
        return
    name, channels = item
    try:
        project = projects.add(name, channels)
    except (ValueError, OSError) as err:  # e.g. made meanwhile from the same scrape
        await back_answer(callback.message, f"Проект не создан: {err}", PROJECTS)
        return
    await views.show_card(callback.message, state, workspaces, project, edit=True)


@router.message(Projects.name, ~F.text.in_(MAIN_MENU))
async def project_name(message: Message, state: FSMContext):
    name = await require_text(message)
    if name is None:
        return
    items = await views.load(message, state)
    if items is None:
        return
    if problem := projects.name_problem(name, items):
        await message.answer(f"Не подходит: {problem}. Пришлите другое имя:")
        return
    await state.update_data(new_name=name)
    await state.set_state(Projects.channels)
    await message.answer(f"{views.CHANNELS_ASK}\n\nНет каналов? Нажмите «{views.NO_CHANNELS['new'][1]}» — база "
                         "будет из участников групп или списка.", reply_markup=views.channels_kb("new"))


@router.message(Projects.channels, ~F.text.in_(MAIN_MENU))
async def project_channels(message: Message, state: FSMContext, workspaces: Workspaces):
    raw = await require_text(message)
    if raw is None:
        return
    channels = await typed_channels(message, raw)
    if channels is not None:
        await _create(message, state, workspaces, channels)


async def _create(message: Message, state: FSMContext, workspaces: Workspaces, channels: list[str]):
    """The new project (its name asked before), then its card; none (`[]`) by its button."""
    try:
        project = projects.add((await state.get_data())["new_name"], channels)
    except (ValueError, OSError) as err:
        await state.clear()
        await back_answer(message, f"Проект не создан: {err}", PROJECTS)
        return
    await views.show_card(message, state, workspaces, project)


@router.callback_query(ChoiceCB.filter(F.scope == "prj_none"))
async def project_without_channels(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext,
                                   workspaces: Workspaces):
    """📭 under the channels' prompt (a new project's, or ✏️ Каналы'): only while the chat is still at its step."""
    if callback_data.value not in views.NO_CHANNELS:
        await callback.answer()
        return
    step, _ = views.NO_CHANNELS[callback_data.value]
    if await left_flow(callback, state, step):
        return
    await callback.answer()
    save = _create if step is Projects.channels else manage.save_channels
    await save(callback.message, state, workspaces, [])
