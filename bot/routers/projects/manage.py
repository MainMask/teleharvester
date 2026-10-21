"""A project's own settings, from its card's screens (card.py): ✏️ its channels, 🏷 its name, 📥 its list,
🧹 clearing its base, 🗑 deleting it; their steps act on the chat's current project (the one whose button
started them: ProjectMiddleware)."""
import asyncio
import html
import secrets

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import ChoiceCB
from bot.keyboards.common import cancel_kb, choice_kb
from bot.routers import mailing
from bot.routers._common import back_answer, read_text_document, require_text, typed_channels
from bot.routers.projects import views
from bot.services.workspaces import Workspaces
from bot.states import Projects
from modules import projects, scraped_files
from modules.registry import COLLECT, MAIN_MENU, MANAGE, menu_path, title

router = Router()

CLEAR_LABELS = {"members": "Участники групп", "list": "Список"}  # what 🧹 offers to clear


# --- the card's screens' buttons (card.py) ---

async def ask_channels(message: Message, state: FSMContext, project: projects.Project):
    await state.set_state(Projects.edit)
    if project.channels:  # by name, as on the card; then the list to copy, and the way to drop it all
        named = projects.chat_list(project, project.channels, html=True, budget=projects.MAX_CHANNELS_TEXT)
        current = html.escape(", ".join(project.channels)[:projects.MAX_CHANNELS_TEXT])
        text = (f"✏️ Каналы — сейчас:\n\n{named}\n\nСписком (нажмите, чтобы скопировать): <code>{current}</code>\n\n"
                f"Новый список заменит текущий. {views.CHANNELS_ASK}")
        markup = views.channels_kb("edit")
    else:
        text, markup = (f"➕ Добавить каналы — сейчас их нет, для скрапа добавьте. {views.CHANNELS_ASK}",
                        cancel_kb(Projects.edit))
    await message.answer(text, parse_mode="HTML", reply_markup=markup)


async def ask_rename(message: Message, state: FSMContext, project: projects.Project):
    await state.set_state(Projects.rename)
    await message.answer(f"🏷 Переименовать — новое имя проекта «{project.name}», его файлы переименуются вместе "
                         "с ним:",
                         reply_markup=cancel_kb(Projects.rename))


async def ask_upload(message: Message, state: FSMContext, project: projects.Project):
    await state.set_state(Projects.upload)
    await message.answer(
        f"{title('upload')} — для проекта «{project.name}»: пришлите .txt файлом или вставьте — по одному "
        "@username, ссылке t.me или номеру на строку. Он добавится к списку проекта, повторы не дублируются; "
        f"рассылка «{title('pmmailing')}» предложит его кнопкой.", reply_markup=cancel_kb(Projects.upload))


async def confirm_delete(message: Message, state: FSMContext, project: projects.Project):
    # the confirmation deletes the project it names, only from the last one shown (its token):
    # any card shown since, or a newer confirmation (B's), makes this one stale
    token = secrets.token_hex(4)
    await state.update_data(delete_name=project.name, delete_token=token)
    await message.edit_text(  # in ⚙️'s place: ↩️ brings it back
        f"🗑 Удалить проект «{project.name}»? Файлы скрапа останутся в {scraped_files.BASES_DIR}. Проект с "
        f"аккаунтами не удаляется — сначала перенесите их ({menu_path('move')}).",
        reply_markup=choice_kb("prj_del", [("🗑 Удалить", f"y{token}"), ("↩️ Отмена", "no")], widths=(2,)))


async def offer_clear(message: Message, state: FSMContext, project: projects.Project):
    """🧹: what the project has to clear, each with its count, in ⚙️'s place; ↩️ brings it back."""
    counts = await asyncio.to_thread(lambda: {
        "members": scraped_files.row_count(scraped_files.members_base(project.name)),
        "list": len(scraped_files.read_list(scraped_files.list_path(project.name)))})
    present = [what for what, path in projects.CLEARABLE.items() if path(project.name).exists()]
    if not present:  # cleared meanwhile, from another chat
        await views.show_screen(message, project, MANAGE)
        return
    # as the delete confirmation: only from the last one shown (its token)
    token = secrets.token_hex(4)
    await state.update_data(clear_name=project.name, clear_token=token)
    await message.edit_text(
        f"🧹 Очистить в проекте «{project.name}»? Файл уйдёт в {scraped_files.BASES_DIR}/{project.name}_previous/ — "
        f"оттуда его можно вернуть (до следующего «{title('topup')}»). Рассылка и контакты его больше не увидят.",
        reply_markup=choice_kb("prj_clr", [*((f"🧹 {CLEAR_LABELS[what]} · {counts[what]}", f"{what}.{token}")
                                             for what in present), ("↩️ Отмена", f"no.{token}")]))


# --- their steps ---

@router.message(Projects.edit, ~F.text.in_(MAIN_MENU))
async def project_edit(message: Message, state: FSMContext, workspaces: Workspaces):
    raw = await require_text(message)
    if raw is None:
        return
    channels = await typed_channels(message, raw)
    if channels is not None:
        await save_channels(message, state, workspaces, channels)


async def save_channels(message: Message, state: FSMContext, workspaces: Workspaces, channels: list[str]):
    """The current project gets `channels` in place of its own (none: `[]`, by its button), then its card."""
    project = await views.current(message, state, workspaces)
    if project is None:
        return
    try:
        project = projects.set_channels(project.name, channels)
    except (ValueError, OSError) as err:
        await state.clear()
        await back_answer(message, f"Не сохранено: {err}", MANAGE, project.id)
        return
    await views.show_card(message, state, workspaces, project)


@router.message(Projects.upload, ~F.text.in_(MAIN_MENU))
async def project_upload(message: Message, state: FSMContext, workspaces: Workspaces):
    if message.document is not None:
        if not (message.document.file_name or "").lower().endswith(".txt"):
            await message.answer("Нужен .txt файл или список текстом. Пришлите ещё раз:")
            return
        text = await read_text_document(message, mailing.MAX_RECIPIENTS_FILE_SIZE)
        if text is None:
            return
    else:
        text = await require_text(message)
        if text is None:
            return
    project = await views.current(message, state, workspaces)
    if project is None:
        return
    try:
        added, total, skipped = await asyncio.to_thread(projects.add_to_list, project.name, text.splitlines())
    except OSError as err:
        await state.clear()
        await back_answer(message, f"Список не сохранён: {err}", COLLECT, project.id)
        return
    if added == 0 and total == 0:  # nothing it could take: asked again
        await message.answer("Ни одного @username или номера в списке. Пришлите ещё раз:")
        return
    note = f" Пропущено строк: {skipped} — не @username и не номер." if skipped else ""
    await views.show_card(message, state, workspaces, project,
                          note=f"✅ В список проекта добавлено: {added}, всего в нём: {total}.{note}")


@router.message(Projects.rename, ~F.text.in_(MAIN_MENU))
async def project_rename(message: Message, state: FSMContext, workspaces: Workspaces):
    new = await require_text(message)
    if new is None:
        return
    data = await state.get_data()
    project = await views.current(message, state, workspaces)
    items = await views.load(message, state) if project is not None else None
    if items is None:
        return
    old, workspace = project.name, workspaces.of_chat(message.chat.id)
    # a members list, a verify or an analysis writes its file under the name it started with (only a
    # scrape leaves a marker projects.rename checks): renamed meanwhile, that file would miss the project
    if busy := next((jobs for jobs in (workspace.scrapes, workspace.analyses) if jobs.active), None):
        await back_answer(message, f"⛔ Идёт «{busy.label}» — переименуйте проект после окончания.", MANAGE,
                          project.id)
        return
    if problem := projects.rename_problem(old, new, items):
        await message.answer(f"Не подходит: {problem}. Пришлите другое имя:")
        return
    try:
        project, moved = projects.rename(old, new)
    except (ValueError, OSError) as err:  # a bot scrape of it, a name taken on disk
        await state.clear()
        await back_answer(message, f"Не переименовано: {err}", MANAGE, workspace.project_id)
        return
    # the list shown before keeps working: its button for the project now carries the new name
    await state.update_data(project_names=[project.name if name == old else name
                                           for name in data.get("project_names") or []])
    await views.show_card(message, state, workspaces, project,
                          note=f"✅ Проект «{old}» теперь «{project.name}». Переименовано файлов и папок: {moved}.")


@router.callback_query(ChoiceCB.filter(F.scope == "prj_clr"))
async def project_clear(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext,
                        workspaces: Workspaces):
    data = await state.get_data()
    what, _, token = callback_data.value.rpartition(".")
    project = await views.find(callback, state, data.get("clear_name") if token == data.get("clear_token") else None)
    if project is None:
        return
    scrapes = workspaces.open(callback.message.chat.id, project).scrapes
    if what != "no" and scrapes.active:  # its members list would add to the base cleared
        await callback.answer(f"⛔ Идёт «{scrapes.label}» — очистите после окончания.", show_alert=True)
        return
    await callback.answer()
    if what == "no":
        await views.show_screen(callback.message, project, MANAGE)
        return
    try:
        moved = projects.clear(project.name, what)
    except (KeyError, ValueError, OSError) as err:
        await back_answer(callback.message, f"Не очищено: {err}", MANAGE, project.id)
        return
    await views.show_card(callback.message, state, workspaces, project, edit=True,
                          note=f"✅ Очищено: {CLEAR_LABELS[what].lower()}. Файл — в {moved}.")


@router.callback_query(ChoiceCB.filter(F.scope == "prj_del"))
async def project_delete(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext,
                         workspaces: Workspaces):
    data = await state.get_data()
    current = callback_data.value in ("no", f"y{data.get('delete_token')}")
    project = await views.find(callback, state, data.get("delete_name") if current else None)
    if project is None:
        return
    workspace = workspaces.get(project.id) if callback_data.value != "no" else None
    if workspace is not None and (busy := next((slot for slot in workspace.slots if slot.active), None)):
        await callback.answer(f"⛔ Идёт «{busy.label}» — удалите проект после окончания.", show_alert=True)
        return
    await callback.answer()
    if callback_data.value == "no":
        await views.show_screen(callback.message, project, MANAGE)
        return
    try:
        projects.remove(project.name)
    except (ValueError, OSError) as err:
        await back_answer(callback.message, f"Не удалено: {err}", MANAGE, project.id)
        return
    workspaces.remove(project.id)
    await views.show_list(callback.message, state, note=f"Проект «{project.name}» удалён, файлы скрапа на месте.",
                          edit=True)
