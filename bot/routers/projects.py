import asyncio

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import ChoiceCB, FunctionCB
from bot.keyboards.common import choice_kb
from bot.keyboards.menu import main_menu
from bot.routers import scraping
from bot.routers._common import require_text
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from bot.states import Projects
from modules import projects, scraped_files
from scraper.scrape import parse_channels

router = Router()

# 📁 Проекты: named sets of scrape sources (modules/projects.py). A project's scrape runs through
# the scrape flow (scraping.scrape_project) under the project's name, so its files, and an
# interrupted scrape to continue, are found by that name.

CHANNELS_ASK = ("Каналы/группы через запятую (@name / t.me ссылка / инвайт t.me/+… / числовой id).\n"
                "Их названия проверяются при скрапе — на выбранном для него аккаунте.")


async def _menu(message: Message, text: str):
    await message.answer(text, reply_markup=main_menu())


async def _load(message: Message, state: FSMContext) -> list | None:
    """The projects; None after telling why (an unreadable file) and back to the menu."""
    try:
        return projects.load()
    except ValueError as err:
        await state.clear()
        await _menu(message, f"⚠️ {err}")
        return None


async def _show_list(message: Message, state: FSMContext, note: str = ""):
    items = await _load(message, state)
    if items is None:
        return
    await state.clear()
    await state.update_data(project_names=[p.name for p in items])  # buttons carry an index into this
    if items:
        text = ("Проекты — сохранённые наборы каналов. Имя проекта — имя его файлов скрапа; "
                "⏸ — есть незавершённый скрап.")
    else:
        text = ("Проектов пока нет. Проект — сохранённый набор каналов: скрап по нему запускается "
                "одной кнопкой, а его файлы видны в карточке проекта.")
    options = [(projects.label(p), str(i)) for i, p in enumerate(items)]
    options.append(("➕ Новый проект", "new"))
    if scraped_files.posts_bases(limit=1):  # which of them can be a project: asked on the button
        options.append(("📥 Из готового скрапа", "import"))
    await message.answer(f"{note}\n\n{text}" if note else text, reply_markup=choice_kb("prj", options))


async def _show_card(message: Message, state: FSMContext, project: projects.Project):
    names = (await state.get_data()).get("project_names")
    await state.clear()
    # the list shown before keeps working: its buttons carry an index into project_names
    await state.update_data(project_name=project.name, project_names=names or [])
    await message.answer(projects.card(project), reply_markup=choice_kb("prj_act", [
        ("▶️ Скрап проекта", "scrape"),
        ("✏️ Каналы", "channels"),
        ("🗑 Удалить", "delete"),
        ("⬅️ К проектам", "back"),
    ]))


async def _project(message: Message, state: FSMContext, name: str | None) -> projects.Project | None:
    """The card's project; None after telling why (a stale button, a project gone meanwhile)."""
    items = await _load(message, state) if name else []
    if items is None:
        return None
    project = next((p for p in items if p.name == name), None)
    if project is None:
        await message.answer("Флоу устарел, откройте 📁 Проекты заново.")
    return project


@router.callback_query(FunctionCB.filter(F.key == "projects"))
async def projects_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await _show_list(callback.message, state)


@router.callback_query(ChoiceCB.filter(F.scope == "prj"))
async def project_pick(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext):
    await callback.answer()
    if callback_data.value == "new":
        await state.set_state(Projects.name)
        await callback.message.answer("Имя проекта — оно же имя его файлов скрапа (например GreenBase). "
                                      "Готовые файлы с этим именем сразу окажутся в проекте:")
        return
    if callback_data.value == "import":
        await _offer_import(callback.message, state)
        return
    names = (await state.get_data()).get("project_names", [])
    index = int(callback_data.value)
    project = await _project(callback.message, state, names[index] if 0 <= index < len(names) else None)
    if project is not None:
        await _show_card(callback.message, state, project)


async def _offer_import(message: Message, state: FSMContext):
    try:  # reads the posts files' channels: off the event loop
        found = await asyncio.to_thread(projects.importable)
    except ValueError as err:  # an unreadable projects file
        await state.clear()
        await _menu(message, f"⚠️ {err}")
        return
    if not found:
        await message.answer("Не из чего сделать проект: у готовых скрапов уже есть проекты, "
                             "или в их файлах не записаны каналы (такой проект создайте через «➕ Новый проект»).")
        return
    await state.update_data(import_items=found)  # buttons carry an index into this
    await message.answer(
        f"Готовые скрапы из {scraped_files.BASES_DIR}: проект получит имя и каналы скрапа, его файлы — "
        "сразу в проекте.",
        reply_markup=choice_kb("prj_imp", [(f"{name} · каналов: {len(channels)}", str(i))
                                           for i, (name, channels) in enumerate(found)]))


@router.callback_query(ChoiceCB.filter(F.scope == "prj_imp"))
async def project_import(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext):
    await callback.answer()
    found = (await state.get_data()).get("import_items", [])
    index = int(callback_data.value)
    if not 0 <= index < len(found):  # a stale button
        await callback.message.answer("Флоу устарел, откройте 📁 Проекты заново.")
        return
    name, channels = found[index]
    try:
        project = projects.add(name, channels)
    except (ValueError, OSError) as err:  # e.g. made meanwhile from the same scrape
        await _menu(callback.message, f"Проект не создан: {err}")
        return
    await _show_card(callback.message, state, project)


@router.message(Projects.name)
async def project_name(message: Message, state: FSMContext):
    name = await require_text(message)
    if name is None:
        return
    items = await _load(message, state)
    if items is None:
        return
    if problem := projects.name_problem(name, items):
        await message.answer(f"Не подходит: {problem}. Пришлите другое имя:")
        return
    await state.update_data(new_name=name)
    await state.set_state(Projects.channels)
    await message.answer(CHANNELS_ASK)


@router.message(Projects.channels)
async def project_channels(message: Message, state: FSMContext):
    raw = await require_text(message)
    if raw is None:
        return
    channels = parse_channels(raw)
    if not channels:
        await message.answer("Не вижу ни одного канала. Пришлите ещё раз:")
        return
    try:
        project = projects.add((await state.get_data())["new_name"], channels)
    except (ValueError, OSError) as err:
        await state.clear()
        await _menu(message, f"Проект не создан: {err}")
        return
    await _show_card(message, state, project)


@router.callback_query(ChoiceCB.filter(F.scope == "prj_act"))
async def project_action(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext,
                         pool: WorkerPool, personal, scrapes: JobManager):
    await callback.answer()
    project = await _project(callback.message, state, (await state.get_data()).get("project_name"))
    if project is None:
        return
    action = callback_data.value
    if action == "scrape":
        await scraping.scrape_project(callback.message, state, project,
                                      dict(pool=pool, personal=personal, scrapes=scrapes))
    elif action == "channels":
        await state.set_state(Projects.edit)
        await callback.message.answer(f"Сейчас: {', '.join(project.channels)[:projects.MAX_CHANNELS_TEXT]}\n\n"
                                      f"Новый список заменит текущий. {CHANNELS_ASK}")
    elif action == "delete":
        await callback.message.answer(
            f"Удалить проект «{project.name}»? Файлы скрапа останутся в {scraped_files.BASES_DIR}.",
            reply_markup=choice_kb("prj_del", [("🗑 Удалить", "yes"), ("Отмена", "no")]))
    elif action == "back":
        await _show_list(callback.message, state)


@router.message(Projects.edit)
async def project_edit(message: Message, state: FSMContext):
    raw = await require_text(message)
    if raw is None:
        return
    channels = parse_channels(raw)
    if not channels:
        await message.answer("Не вижу ни одного канала. Пришлите ещё раз:")
        return
    try:
        project = projects.set_channels((await state.get_data())["project_name"], channels)
    except (ValueError, OSError) as err:
        await state.clear()
        await _menu(message, f"Не сохранено: {err}")
        return
    await _show_card(message, state, project)


@router.callback_query(ChoiceCB.filter(F.scope == "prj_del"))
async def project_delete(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext):
    await callback.answer()
    project = await _project(callback.message, state, (await state.get_data()).get("project_name"))
    if project is None:
        return
    if callback_data.value != "yes":
        await _show_card(callback.message, state, project)
        return
    try:
        projects.remove(project.name)
    except (ValueError, OSError) as err:
        await _menu(callback.message, f"Не удалено: {err}")
        return
    await _show_list(callback.message, state, note=f"Проект «{project.name}» удалён, файлы скрапа на месте.")
