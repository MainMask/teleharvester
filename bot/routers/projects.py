import asyncio
import html
import secrets

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import ChoiceCB
from bot.keyboards.common import choice_kb
from bot.keyboards.menu import PROJECTS_BUTTON, main_menu
from bot.routers import audience, broadcasts, scraping
from bot.routers._common import ensure_workers, require_text
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
        text = ("📁 <b>Проекты</b> — ваши аудитории: каналы, сбор и вся работа с их базами (рассылка, контакты, "
                "проверка, анализ). Имя проекта — имя его файлов скрапа; ⏸ — есть незавершённый скрап.")
    else:
        text = ("📁 <b>Проекты</b> — пока ни одного. Проект — набор каналов: скрап по нему одной кнопкой, "
                "а в карточке — вся работа с собранной базой.")
    options = [(projects.label(p), str(i)) for i, p in enumerate(items)]
    options.append(("➕ Новый проект", "new"))
    if scraped_files.posts_bases(limit=1):  # which of them can be a project: asked on the button
        options.append(("📥 Из готового скрапа", "import"))
    await message.answer(f"{html.escape(note)}\n\n{text}" if note else text, parse_mode="HTML",
                         reply_markup=choice_kb("prj", options))


async def _show_card(message: Message, state: FSMContext, project: projects.Project):
    names = (await state.get_data()).get("project_names")
    await state.clear()
    # the list shown before keeps working: its buttons carry an index into project_names
    await state.update_data(project_name=project.name, project_names=names or [])
    # a top-up while a scrape is unfinished would drop its checkpoint: ▶️ continues that first
    topup = projects.topup(project) is not None and projects.unfinished(project.name) is None
    # the work on the project's data: each button runs the section's own flow on its files only
    base = bool(scraped_files.project_files(project.name, kinds=("participants",)))
    posts = bool(scraped_files.project_files(project.name, kinds=("posts",)))
    await message.answer(projects.card(project), reply_markup=choice_kb("prj_act", [
        ("▶️ Скрап проекта", "scrape"),
        *([("🔄 Дособрать", "topup")] if topup else []),
        *([("📨 Рассылка по базе", "mail"), ("📇 В контакты", "contacts")] if base else []),
        *([("✅ Верификация", "verify"), ("📈 Анализ", "analysis")] if posts else []),
        ("✏️ Каналы", "channels"),
        ("🏷 Переименовать", "rename"),
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


@router.message(F.text == PROJECTS_BUTTON)
async def projects_menu(message: Message, state: FSMContext):
    await _show_list(message, state)


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
        reply_markup=choice_kb("prj_imp", [(f"📁 {name} · каналов: {len(channels)}", str(i))
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
    elif action == "topup":
        plan = projects.topup(project)
        if plan is None:  # its posts file gone meanwhile
            await callback.message.answer("Дособирать не к чему: у проекта нет готового файла постов.")
            return
        await scraping.topup_project(callback.message, state, project, plan,
                                     dict(pool=pool, personal=personal, scrapes=scrapes))
    elif action in ("mail", "contacts"):  # the project's bases only: the base is still picked by button
        if not await ensure_workers(callback, pool):
            return
        bases = scraped_files.project_files(project.name, kinds=("participants",))
        ask = broadcasts.ask_recipients if action == "mail" else audience.ask_base
        await ask(callback.message, state, bases)
    elif action == "verify":
        await scraping.verify_project(callback.message, state, project,
                                      dict(pool=pool, personal=personal, scrapes=scrapes))
    elif action == "analysis":
        await scraping.analysis_project(callback.message, state, project)
    elif action == "channels":
        await state.set_state(Projects.edit)
        await callback.message.answer(f"Сейчас: {', '.join(project.channels)[:projects.MAX_CHANNELS_TEXT]}\n\n"
                                      f"Новый список заменит текущий. {CHANNELS_ASK}")
    elif action == "rename":
        await state.set_state(Projects.rename)
        await callback.message.answer(f"Новое имя проекта «{project.name}» — файлы проекта переименуются "
                                      "вместе с ним:")
    elif action == "delete":
        # the confirmation deletes the project it names, only from the last one shown (its token):
        # any card shown since, or a newer confirmation (B's), makes this one stale
        token = secrets.token_hex(4)
        await state.update_data(delete_name=project.name, delete_token=token)
        await callback.message.answer(
            f"Удалить проект «{project.name}»? Файлы скрапа останутся в {scraped_files.BASES_DIR}.",
            reply_markup=choice_kb("prj_del", [("🗑 Удалить", f"y{token}"), ("↩️ Отмена", "no")]))
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


@router.message(Projects.rename)
async def project_rename(message: Message, state: FSMContext):
    new = await require_text(message)
    if new is None:
        return
    data = await state.get_data()
    items = await _load(message, state)
    if items is None:
        return
    old = data.get("project_name")
    if old not in [p.name for p in items]:  # gone meanwhile
        await state.clear()
        await _menu(message, "Флоу устарел, откройте 📁 Проекты заново.")
        return
    if problem := projects.rename_problem(old, new, items):
        await message.answer(f"Не подходит: {problem}. Пришлите другое имя:")
        return
    try:
        project, moved = projects.rename(old, new)
    except (ValueError, OSError) as err:  # a bot scrape of it, a name taken on disk
        await state.clear()
        await _menu(message, f"Не переименовано: {err}")
        return
    # the list shown before keeps working: its button for the project now carries the new name
    await state.update_data(project_names=[project.name if name == old else name
                                           for name in data.get("project_names") or []])
    await message.answer(f"✅ Проект «{old}» теперь «{project.name}». Переименовано файлов и папок: {moved}.")
    await _show_card(message, state, project)


@router.callback_query(ChoiceCB.filter(F.scope == "prj_del"))
async def project_delete(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    current = callback_data.value in ("no", f"y{data.get('delete_token')}")
    project = await _project(callback.message, state, data.get("delete_name") if current else None)
    if project is None:
        return
    if callback_data.value == "no":
        await _show_card(callback.message, state, project)
        return
    try:
        projects.remove(project.name)
    except (ValueError, OSError) as err:
        await _menu(callback.message, f"Не удалено: {err}")
        return
    await _show_list(callback.message, state, note=f"Проект «{project.name}» удалён, файлы скрапа на месте.")
