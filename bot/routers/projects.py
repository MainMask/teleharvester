import asyncio
import html
import secrets
from typing import NamedTuple

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from bot.callbacks import ChoiceCB
from bot.keyboards.common import cancel_kb, choice_kb, paged_kb
from bot.keyboards.menu import card_kb
from bot.routers import audience, broadcasts, scraping
from bot.routers._common import (ensure_workers, left_flow, menu_answer, read_text_document, require_text,
                                 typed_channels)
from bot.services import screens
from bot.services.workspaces import Workspaces
from modules.registry import (CARD, MAIN_MENU, PROJECT_CARD, PROJECT_MANAGE, PROJECTS, RISK_NOTE, card_rows, children,
                              menu_path)
from bot.states import Projects
from modules import projects, scraped_files

router = Router()

# 📁 Проекты: named sets of scrape sources (modules/projects.py). A project's scrape runs through
# the scrape flow (scraping.scrape_project) under the project's name, so its files, and an
# interrupted scrape to continue, are found by that name. All the work is a project's, on its own
# workers: its card makes it the chat's current project (bot/services/workspaces.py), whose workers
# the card's functions and its 🤖 Аккаунты, 💬 Активность run on.

CHANNELS_ASK = ("Каналы/группы через запятую (@name / t.me ссылка / инвайт t.me/+… / числовой id).\n"
                "Их названия проверяются при скрапе — на выбранном для него аккаунте.")
# a project without channels: by its button under the channels' prompt, never typed
NO_CHANNELS = {"new": (Projects.channels, "📭 Без каналов"), "edit": (Projects.edit, "📭 Убрать все каналы")}


def _channels_kb(step: str):
    """The channels' prompt's buttons: the one for a project without them, ✖️ Отмена."""
    state, label = NO_CHANNELS[step]
    return cancel_kb(state, choice_kb("prj_none", [(label, step)]))


async def _load(message: Message, state: FSMContext) -> list | None:
    """The projects; None after telling why (an unreadable file) and back to the menu."""
    try:
        return projects.load()
    except ValueError as err:
        await state.clear()
        await menu_answer(message, f"⚠️ {err}")
        return None


async def _show_list(message: Message, state: FSMContext, note: str = "", edit: bool = False, page: int = 0):
    """The projects' list, a `page` of them (keyboards.paged_kb); `edit`: in place of the message (a
    button's), not a new one."""
    items = await _load(message, state)
    if items is None:
        return
    await state.clear()
    await state.update_data(project_names=[p.name for p in items])  # buttons carry an index into this
    if items:
        text = ("📁 <b>Проекты</b> — у каждого свои аккаунты и прокси, своя база и вся работа с ней: сбор, "
                "рассылка, контакты, проверка, анализ, активность воркеров — в карточке проекта. Имя проекта — "
                "имя его файлов скрапа; ⏸ — есть незавершённый скрап.")
    else:
        text = ("📁 <b>Проекты</b> — пока ни одного. У проекта свои аккаунты и прокси и своя аудитория: каналы "
                "для скрапа, участники групп или список; в его карточке — сбор и вся работа с базой, аккаунты и "
                "их активность. Готовые файлы скрапа станут проектом через «📥 Из готового скрапа».")
    listed = paged_kb("prj", [(projects.label(p), str(i)) for i, p in enumerate(items)], page)
    actions = [("➕ Новый проект", "new")]
    if scraped_files.scrape_names(limit=1):  # which of them can be a project: asked on the button
        actions.append(("📥 Из готового скрапа", "import"))
    markup = InlineKeyboardMarkup(inline_keyboard=[*listed.inline_keyboard,
                                                   *choice_kb("prj", actions).inline_keyboard])
    send = message.edit_text if edit else message.answer
    await send(f"{html.escape(note)}\n\n{text}" if note else text, parse_mode="HTML", reply_markup=markup)


class _CardView(NamedTuple):
    """The card's text, and its 🔄: the functions are always there (projects.missing)."""
    text: str
    topup: bool  # a finished scrape to bring up to date


def _card_view(project: projects.Project, workers: int) -> _CardView:
    """The card as shown: reads the files' footers (and the posts file's attrs), so off the event loop."""
    # a top-up while a scrape is unfinished would drop its checkpoint: ▶️ continues that first
    topup = projects.topup(project) is not None and projects.unfinished(project.name) is None
    return _CardView(projects.card(project, workers, html=True), topup)


async def _show_card(message: Message, state: FSMContext, workspaces: Workspaces, project: projects.Project,
                     edit: bool = False, note: str = ""):
    """The project's card, the chat's current project from now on; `edit`: in place of the message (a
    button's), not a new one; `note` above it."""
    names = (await state.get_data()).get("project_names")
    await state.clear()
    # the list shown before keeps working: its buttons carry an index into project_names; the card's
    # carry its token, as the delete confirmation: an older card's must not act on the one opened since
    token = secrets.token_hex(4)
    await state.update_data(project_name=project.name, project_names=names or [], card_token=token)
    workspace = workspaces.open(message.chat.id, project)
    view = await asyncio.to_thread(_card_view, project, workspace.pool.count())
    text = f"{html.escape(note)}\n\n{view.text}" if note else view.text
    text += f"\n\n<i>{RISK_NOTE}</i>"  # what its ⚠️ buttons mean
    # every function, data or not — one with nothing to work on yet says what to collect first
    # (projects.missing) —, then ⚙️ to the project itself, its accounts and their activity; 🔄 only
    # when there is something to top up
    rows = card_rows(PROJECT_CARD, hidden=() if view.topup else ("topup",))
    send = message.edit_text if edit else message.answer
    await send(text, parse_mode="HTML",
               reply_markup=card_kb(rows + [[("⬅️ Назад", "back")]], token, tuple(children(CARD))))


async def _show_manage(message: Message, state: FSMContext, project: projects.Project):
    """⚙️ Управление проектом in the card's place, with its token: ⬅️ brings the card back; 🧹 only
    when there is something to clear (a group members' base or a list: only ever added to)."""
    clear = await asyncio.to_thread(lambda: any(path(project.name).exists() for path in projects.CLEARABLE.values()))
    shown = dict(hidden=() if clear else ("clear",), channels=bool(project.channels))
    token = (await state.get_data())["card_token"]
    await message.edit_text(f"⚙️ <b>Управление проектом «{html.escape(project.name)}»</b>\n\n"
                            f"{screens.card_text(PROJECT_MANAGE, **shown)}", parse_mode="HTML",
                            reply_markup=card_kb(card_rows(PROJECT_MANAGE, **shown) + [[("⬅️ Назад", "card")]], token))


async def _project(message: Message, state: FSMContext, name: str | None) -> projects.Project | None:
    """The card's project; None after telling why (a stale button, a project gone meanwhile)."""
    items = await _load(message, state) if name else []
    if items is None:
        return None
    project = next((p for p in items if p.name == name), None)
    if project is None:
        await message.answer("Кнопка устарела — откройте 📁 Проекты заново.")
    return project


@screens.view(PROJECTS)
async def projects_screen(message: Message, state: FSMContext, edit: bool, **_):
    await _show_list(message, state, edit=edit)


@screens.view(CARD)  # ⬅️ Назад from its screens: the current project's card (menu._open checks there is one)
async def card_screen(message: Message, state: FSMContext, edit: bool, workspace, workspaces: Workspaces, **_):
    items = await _load(message, state)
    if items is None:
        return
    project = next((p for p in items if p.id == workspace.project_id), None)
    if project is None:  # deleted meanwhile, from another chat
        await _show_list(message, state, edit=edit)
        return
    await _show_card(message, state, workspaces, project, edit=edit)


@router.callback_query(ChoiceCB.filter(F.scope == "prj"))
async def project_pick(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext, workspaces: Workspaces):
    await callback.answer()
    if callback_data.value == "new":
        await state.set_state(Projects.name)
        await callback.message.answer("Имя проекта — оно же имя его файлов скрапа (например GreenBase). "
                                      "Готовые файлы с этим именем сразу окажутся в проекте:",
                                      reply_markup=cancel_kb(Projects.name))
        return
    if callback_data.value == "import":
        await _offer_import(callback.message, state)
        return
    if callback_data.value.startswith("pg."):  # ⬅️ · 📄 · ➡️: another page, the list read again
        try:
            await _show_list(callback.message, state, edit=True, page=int(callback_data.value[3:]))
        except Exception:  # 📄, the page shown: "message is not modified"
            pass
        return
    names = (await state.get_data()).get("project_names", [])
    index = int(callback_data.value)
    project = await _project(callback.message, state, names[index] if 0 <= index < len(names) else None)
    if project is not None:
        await _show_card(callback.message, state, workspaces, project, edit=True)


async def _offer_import(message: Message, state: FSMContext):
    try:  # reads the posts files' channels: off the event loop
        found = await asyncio.to_thread(projects.importable)
    except ValueError as err:  # an unreadable projects file
        await state.clear()
        await menu_answer(message, f"⚠️ {err}")
        return
    if not found:
        await message.answer("Не из чего сделать проект: у готовых скрапов уже есть проекты, "
                             "или в их файлах не записаны каналы (такой проект создайте через «➕ Новый проект»).")
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
    await callback.answer()
    if callback_data.value == "back":
        await _show_list(callback.message, state, edit=True)
        return
    found = (await state.get_data()).get("import_items", [])
    index = int(callback_data.value)
    if not 0 <= index < len(found):  # a stale button
        await callback.message.answer("Кнопка устарела — откройте 📁 Проекты заново.")
        return
    name, channels = found[index]
    try:
        project = projects.add(name, channels)
    except (ValueError, OSError) as err:  # e.g. made meanwhile from the same scrape
        await menu_answer(callback.message, f"Проект не создан: {err}")
        return
    await _show_card(callback.message, state, workspaces, project, edit=True)


@router.message(Projects.name, ~F.text.in_(MAIN_MENU))
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
    await message.answer(f"{CHANNELS_ASK}\n\nНет каналов? Нажмите «{NO_CHANNELS['new'][1]}» — база будет из "
                         "участников групп или списка.", reply_markup=_channels_kb("new"))


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
        await menu_answer(message, f"Проект не создан: {err}")
        return
    await _show_card(message, state, workspaces, project)


MISSING_CHECKED = ("scrape", "contacts", "mail", "verify", "analysis")  # the card's functions on its data


@router.callback_query(ChoiceCB.filter(F.scope == "prj_act"))
async def project_action(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext,
                         workspaces: Workspaces, personal):
    data = await state.get_data()
    action, _, token = callback_data.value.rpartition(".")
    project = await _project(callback.message, state,
                             data.get("project_name") if token == data.get("card_token") else None)
    if project is None:
        await callback.answer()
        return
    # a function with nothing to work on yet: what to collect first, in a pop-up over the card
    why = await asyncio.to_thread(projects.missing, project, action) if action in MISSING_CHECKED else None
    await callback.answer(why, show_alert=bool(why))
    if why:
        return
    workspace = workspaces.open(callback.message.chat.id, project)  # the card's: its workers, its slots
    pool, scrapes = workspace.pool, workspace.scrapes
    if action == "scrape":
        await scraping.scrape_project(callback.message, state, project,
                                      dict(pool=pool, personal=personal, scrapes=scrapes))
    elif action == "topup":
        plan = await asyncio.to_thread(projects.topup, project)  # reads the posts file's footer
        if plan is None:  # its posts file gone meanwhile
            await callback.message.answer("Дособирать не к чему: у проекта нет готового файла постов.")
            return
        await scraping.topup_project(callback.message, state, project, plan,
                                     dict(pool=pool, personal=personal, scrapes=scrapes))
    elif action == "members":
        await scraping.members_project(callback.message, state, project,
                                       dict(pool=pool, personal=personal, scrapes=scrapes))
    elif action in ("mail", "contacts"):  # the project's bases only: the base is still picked by button
        if not await ensure_workers(callback, pool):
            return
        kinds = ("participants", "list") if action == "mail" else ("participants",)
        bases = scraped_files.project_files(project.name, kinds=kinds)
        ask = broadcasts.ask_recipients if action == "mail" else audience.ask_base
        await ask(callback.message, state, bases)
    elif action == "upload":
        await state.set_state(Projects.upload)
        await callback.message.answer(
            f"Список для проекта «{project.name}»: пришлите .txt файлом или вставьте — по одному @username, "
            "ссылке t.me или номеру на строку. Он добавится к списку проекта, повторы не дублируются; "
            "рассылка «📨 ЛС по базе» предложит его кнопкой.", reply_markup=cancel_kb(Projects.upload))
    elif action == "verify":
        await scraping.verify_project(callback.message, state, project,
                                      dict(pool=pool, personal=personal, scrapes=scrapes))
    elif action == "analysis":
        await scraping.analysis_project(callback.message, state, project)
    elif action == "channels":
        await state.set_state(Projects.edit)
        if project.channels:  # by name, as on the card; then the list to copy, and the way to drop it all
            named = projects.chat_list(project, project.channels, html=True, budget=projects.MAX_CHANNELS_TEXT)
            current = html.escape(", ".join(project.channels)[:projects.MAX_CHANNELS_TEXT])
            text = (f"Сейчас:\n\n{named}\n\nСписком (нажмите, чтобы скопировать): <code>{current}</code>\n\n"
                    f"Новый список заменит текущий. {CHANNELS_ASK}")
            markup = _channels_kb("edit")
        else:
            text, markup = f"Сейчас каналов нет — для скрапа добавьте их. {CHANNELS_ASK}", cancel_kb(Projects.edit)
        await callback.message.answer(text, parse_mode="HTML", reply_markup=markup)
    elif action == "rename":
        await state.set_state(Projects.rename)
        await callback.message.answer(f"Новое имя проекта «{project.name}» — файлы проекта переименуются "
                                      "вместе с ним:", reply_markup=cancel_kb(Projects.rename))
    elif action == "delete":
        # the confirmation deletes the project it names, only from the last one shown (its token):
        # any card shown since, or a newer confirmation (B's), makes this one stale
        token = secrets.token_hex(4)
        await state.update_data(delete_name=project.name, delete_token=token)
        await callback.message.edit_text(  # in ⚙️'s place: ↩️ brings it back
            f"Удалить проект «{project.name}»? Файлы скрапа останутся в {scraped_files.BASES_DIR}. Проект с "
            f"аккаунтами не удаляется — сначала перенесите их ({menu_path('move')}).",
            reply_markup=choice_kb("prj_del", [("🗑 Удалить", f"y{token}"), ("↩️ Отмена", "no")], widths=(2,)))
    elif action == "clear":
        await _offer_clear(callback.message, state, project)
    elif action == "manage":
        await _show_manage(callback.message, state, project)
    elif action == "card":
        await _show_card(callback.message, state, workspaces, project, edit=True)
    elif action == "back":
        await _show_list(callback.message, state, edit=True)


@router.message(Projects.edit, ~F.text.in_(MAIN_MENU))
async def project_edit(message: Message, state: FSMContext, workspaces: Workspaces):
    raw = await require_text(message)
    if raw is None:
        return
    channels = await typed_channels(message, raw)
    if channels is not None:
        await _save_channels(message, state, workspaces, channels)


async def _save_channels(message: Message, state: FSMContext, workspaces: Workspaces, channels: list[str]):
    """The card's project gets `channels` in place of its own (none: `[]`, by its button), then its card."""
    try:
        project = projects.set_channels((await state.get_data())["project_name"], channels)
    except (ValueError, OSError) as err:
        await state.clear()
        await menu_answer(message, f"Не сохранено: {err}")
        return
    await _show_card(message, state, workspaces, project)


@router.message(Projects.upload, ~F.text.in_(MAIN_MENU))
async def project_upload(message: Message, state: FSMContext, workspaces: Workspaces):
    if message.document is not None:
        if not (message.document.file_name or "").lower().endswith(".txt"):
            await message.answer("Нужен .txt файл или список текстом. Пришлите ещё раз:")
            return
        text = await read_text_document(message, broadcasts.MAX_RECIPIENTS_FILE_SIZE)
        if text is None:
            return
    else:
        text = await require_text(message)
        if text is None:
            return
    project = await _project(message, state, (await state.get_data()).get("project_name"))
    if project is None:
        return
    try:
        added, total, skipped = await asyncio.to_thread(projects.add_to_list, project.name, text.splitlines())
    except OSError as err:
        await state.clear()
        await menu_answer(message, f"Список не сохранён: {err}")
        return
    if added == 0 and total == 0:  # nothing it could take: asked again
        await message.answer("Ни одного @username или номера в списке. Пришлите ещё раз:")
        return
    note = f" Пропущено строк: {skipped} — не @username и не номер." if skipped else ""
    await message.answer(f"✅ В список проекта добавлено: {added}, всего в нём: {total}.{note}")
    await _show_card(message, state, workspaces, project)


@router.callback_query(ChoiceCB.filter(F.scope == "prj_none"))
async def project_without_channels(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext,
                                   workspaces: Workspaces):
    """📭 under the channels' prompt: only while the chat is still at that prompt's step."""
    if callback_data.value not in NO_CHANNELS:
        await callback.answer()
        return
    step, _ = NO_CHANNELS[callback_data.value]
    if await left_flow(callback, state, step):
        return
    await callback.answer()
    save = _create if step is Projects.channels else _save_channels
    await save(callback.message, state, workspaces, [])


@router.message(Projects.rename, ~F.text.in_(MAIN_MENU))
async def project_rename(message: Message, state: FSMContext, workspaces: Workspaces):
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
        await menu_answer(message, "Действие устарело — откройте 📁 Проекты заново.")
        return
    workspace = workspaces.open(message.chat.id, next(p for p in items if p.name == old))
    # a members list, a verify or an analysis writes its file under the name it started with (only a
    # scrape leaves a marker projects.rename checks): renamed meanwhile, that file would miss the project
    if busy := next((jobs for jobs in (workspace.scrapes, workspace.analyses) if jobs.active), None):
        await message.answer(f"⛔ Идёт «{busy.label}» — переименуйте проект после её окончания.")
        return
    if problem := projects.rename_problem(old, new, items):
        await message.answer(f"Не подходит: {problem}. Пришлите другое имя:")
        return
    try:
        project, moved = projects.rename(old, new)
    except (ValueError, OSError) as err:  # a bot scrape of it, a name taken on disk
        await state.clear()
        await menu_answer(message, f"Не переименовано: {err}")
        return
    # the list shown before keeps working: its button for the project now carries the new name
    await state.update_data(project_names=[project.name if name == old else name
                                           for name in data.get("project_names") or []])
    await message.answer(f"✅ Проект «{old}» теперь «{project.name}». Переименовано файлов и папок: {moved}.")
    await _show_card(message, state, workspaces, project)


CLEAR_LABELS = {"members": "Участники групп", "list": "Список"}  # what 🧹 offers to clear


async def _offer_clear(message: Message, state: FSMContext, project: projects.Project):
    """🧹: what the project has to clear, each with its count, in ⚙️'s place; ↩️ brings it back."""
    counts = await asyncio.to_thread(lambda: {
        "members": scraped_files.row_count(scraped_files.members_base(project.name)),
        "list": len(scraped_files.read_list(scraped_files.list_path(project.name)))})
    present = [what for what, path in projects.CLEARABLE.items() if path(project.name).exists()]
    if not present:  # cleared meanwhile, from another card
        await _show_manage(message, state, project)
        return
    # as the delete confirmation: only from the last one shown (its token)
    token = secrets.token_hex(4)
    await state.update_data(clear_name=project.name, clear_token=token)
    await message.edit_text(
        f"Очистить в проекте «{project.name}»? Файл уйдёт в {scraped_files.BASES_DIR}/{project.name}_previous/ — "
        "оттуда его можно вернуть (до следующего «🔄 Дособрать»). Рассылка и контакты его больше не увидят.",
        reply_markup=choice_kb("prj_clr", [*((f"🧹 {CLEAR_LABELS[what]} · {counts[what]}", f"{what}.{token}")
                                             for what in present), ("↩️ Отмена", f"no.{token}")]))


@router.callback_query(ChoiceCB.filter(F.scope == "prj_clr"))
async def project_clear(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext,
                        workspaces: Workspaces):
    await callback.answer()
    data = await state.get_data()
    what, _, token = callback_data.value.rpartition(".")
    project = await _project(callback.message, state, data.get("clear_name") if token == data.get("clear_token")
                             else None)
    if project is None:
        return
    if what == "no":
        await _show_manage(callback.message, state, project)
        return
    scrapes = workspaces.open(callback.message.chat.id, project).scrapes
    if scrapes.active:  # a members list of the project would be added to the base it clears
        await callback.message.answer(f"⛔ Идёт «{scrapes.label}» — очистите после её окончания.")
        return
    try:
        moved = projects.clear(project.name, what)
    except (KeyError, ValueError, OSError) as err:
        await callback.message.answer(f"Не очищено: {err}")
        return
    await _show_card(callback.message, state, workspaces, project, edit=True,
                     note=f"✅ Очищено: {CLEAR_LABELS[what].lower()}. Файл — в {moved}.")


@router.callback_query(ChoiceCB.filter(F.scope == "prj_del"))
async def project_delete(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext,
                         workspaces: Workspaces):
    await callback.answer()
    data = await state.get_data()
    current = callback_data.value in ("no", f"y{data.get('delete_token')}")
    project = await _project(callback.message, state, data.get("delete_name") if current else None)
    if project is None:
        return
    if callback_data.value == "no":
        await _show_manage(callback.message, state, project)
        return
    workspace = workspaces.get(project.id)
    if workspace is not None and (busy := next((slot for slot in workspace.slots if slot.active), None)):
        await callback.message.answer(f"⛔ Идёт «{busy.label}» — удалите проект после её окончания.")
        return
    try:
        projects.remove(project.name)
    except (ValueError, OSError) as err:
        await menu_answer(callback.message, f"Не удалено: {err}")
        return
    workspaces.remove(project.id)
    await _show_list(callback.message, state, note=f"Проект «{project.name}» удалён, файлы скрапа на месте.",
                     edit=True)
