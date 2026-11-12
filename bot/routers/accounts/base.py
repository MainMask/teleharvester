"""🤖 Аккаунты' screen (its actions, the registry's) and 📋 Список: its checks (the registry's; the workers' poll:
workers.py), the project's account files by category — new, warming, warmed, failed (modules/warmup.py),
permanently restricted, dead (sessions/inactive/) —, the personal accounts: a category's accounts with their
cards (card.py), their ZIP, their deletion; the whole base's ZIP."""
import asyncio
import html
import io
import os
import tempfile
import zipfile
from collections import Counter

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.callbacks import AccountBaseCB, MenuAction, MenuCB
from bot.keyboards.common import button
from bot.keyboards.menu import back_kb, nav_button, screen_kb
from bot.routers._common import MAX_MESSAGE, confirm_expired, fit_lines
from bot.routers.accounts import card, workers
from bot.services import screens
from bot.services.screens import head
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from bot.services.workspaces import Workspace, delete_workers
from modules import account_age, projects, restricted_workers, session_import, warmup
from modules.registry import ACCOUNT_LIST, ACCOUNTS, ADD
from modules.scraper_creds import scrape_accounts

router = Router()

SHOWN_ACCOUNTS = 50  # a category's accounts listed in its message
PERSONAL = "personal"  # the personal accounts' button: theirs are cards only, they are no project's files
PERSONAL_TITLE = card.PERSONAL_TITLE
EMPTY_CATEGORY = "В этой категории нет аккаунтов."
WHOLE_BASE = "📦 Все аккаунты"
ZIP = "📦 Скачать ZIP"  # a category's
# 📋 Список's own buttons, each with what it does (screens.screen_text's `own`); the categories: the two not named
# by the warmup
OWN_BUTTONS = {"cat": ("Категории", "карточки аккаунтов (код входа), 📦 ZIP, 🗑 удаление; ⛔ — по проверке SpamBot, "
                                    "💀 — мёртвые сессии: бан или вылет (нужен новый вход)"),
               PERSONAL: (PERSONAL_TITLE, "общие для всех проектов, только для скрапа: карточки, удаление"),
               "zip": (WHOLE_BASE, "ZIP всех аккаунтов проекта")}
# a ZIP's formats, each with what it is for
FORMATS = {"session": ("📦 .session + .json", "для другого софта: ключ, устройство и приложение, пароль 2FA"),
           "jsession": ("📦 .jsession", "резервная копия для этого бота: всё, вместе с прокси")}
SAME_KEY = ("В архиве — те же ключи входа, что у бота: не запускайте аккаунт одновременно здесь и в другом месте, "
            "иначе Telegram может завершить сессию.")


def _button(text: str, action: str, value: str, project: int) -> InlineKeyboardButton:
    return button(text, AccountBaseCB(action=action, value=value, project=project))


def _paths(pool: WorkerPool, name: str) -> list[str]:
    """The files of a category (modules/warmup.CATEGORIES), or of the whole base ("all"): live and dead."""
    found = warmup.categories(pool.storage)
    return [path for paths in found.values() for path in paths] if name == "all" else found.get(name, [])


def _personal(personal) -> list:
    return [account for account in scrape_accounts(None, personal) if account.personal]


@screens.view(ACCOUNTS)
async def accounts_screen(message: Message, state: FSMContext, edit: bool, pool: WorkerPool, workspace: Workspace,
                          **_):
    """The workers' count and warmup (modules/warmup.summary, as the card has it)."""
    project, count = workspace.project_id, pool.count()
    summary = warmup.summary(pool.storage) if count else ""
    workers_line = f"Воркеров: <b>{count}</b>" + (f" · {html.escape(summary)}" if summary else "") if count else \
        f"Воркеров нет — добавьте их кнопкой «{html.escape(ADD)}»."
    header = f"{screens.title_line(ACCOUNTS, screens.project_name(project))}\n\n{workers_line}"
    await screens.show(message, ACCOUNTS, edit, header, project)


@screens.view(ACCOUNT_LIST)
async def list_screen(message: Message, state: FSMContext, edit: bool, pool: WorkerPool, workspace: Workspace,
                      personal, **_):
    """What there is only: 📡 and 🩺 with workers, the categories with accounts, the personal ones, the ZIP with
    files."""
    project = workspace.project_id
    found, own = warmup.categories(pool.storage), _personal(personal)
    categories = [_button(f"{warmup.CATEGORIES[name]} · {len(paths)}", "cat", name, project)
                  for name, paths in found.items() if paths]
    rows = [categories[i:i + 2] for i in range(0, len(categories), 2)]
    hints = [OWN_BUTTONS["cat"]] if categories else []
    if own:
        rows.append([_button(f"{PERSONAL_TITLE} · {len(own)}", "cat", PERSONAL, project)])
        hints.append(OWN_BUTTONS[PERSONAL])
    if categories:
        rows.append([_button(WHOLE_BASE, "zip", "all", project)])
        hints.append(OWN_BUTTONS["zip"])
    header = screens.title_line(ACCOUNT_LIST, screens.project_name(project))
    if not rows:
        header += f"\n\nАккаунтов нет — добавьте их: «{html.escape(ACCOUNTS)}» → «{html.escape(ADD)}»."
    hidden = () if pool.count() else (MenuAction.POLL.value, "status")
    await screens.send(message, edit)(screens.screen_text(ACCOUNT_LIST, header, hidden, own=hints), parse_mode="HTML",
                                      reply_markup=screen_kb(ACCOUNT_LIST, project, hidden, own=rows))


@router.callback_query(AccountBaseCB.filter(F.action == "cat"))
async def category_open(callback: CallbackQuery, callback_data: AccountBaseCB, state: FSMContext, pool: WorkerPool,
                        workspace: Workspace, personal):
    """A category's accounts in the screen's place: their cards, its ZIP and 🗑 (the personal ones': cards only)."""
    name, project = callback_data.value, workspace.project_id
    if name == PERSONAL:
        own = _personal(personal)
        if not own:
            await callback.answer("Личных аккаунтов нет.", show_alert=True)
            return
        await callback.answer()
        text = f"{screens.title_line(ACCOUNT_LIST, screens.project_name(project))}\n\n{card.personal_text(own)}" \
               f"\n\n{card.CARDS_HINT}"
        await card.offer_cards(callback.message, state, own, text, card.back_to_list(project), project, edit=True)
        return
    paths = _paths(pool, name)
    if name not in warmup.CATEGORIES or not paths:
        await callback.answer(EMPTY_CATEGORY, show_alert=True)
        return
    await callback.answer()
    errors = warmup.load_state() if name == warmup.FAILED else {}
    causes = {path: restricted_workers.CAUSES.get(cause, "")  # a dead one's, if the status check found it
              for path, cause in restricted_workers.load_dead().items()} if name == warmup.DEAD else {}
    lines = []
    for path in paths[:SHOWN_ACCOUNTS]:
        error = errors.get(warmup.key(pool.storage, path), {}).get("error") or causes.get(path)
        account = projects.read_account(path) or {}
        age = account_age.label(account.get("registered_at"), account.get("user_id"))
        lines.append(f"• {html.escape(projects.account_label(path))}" + (f" · 🎂 {age}" if age else "")
                     + (f" — {html.escape(error)}" if error else ""))
    # the live ones' cards (a dead one's file is no session of the project's any more)
    live = [account for account in scrape_accounts(pool.storage) if account.path in set(paths)]
    known = any(causes.get(path) for path in paths)  # none known (all found dead before causes were kept): no split
    counts = Counter(causes.get(path) or "причина неизвестна" for path in paths) if known else {}
    split = f" ({' · '.join(f'{cause} {n}' for cause, n in counts.items())})" if counts else ""
    top = f"{screens.title_line(ACCOUNT_LIST, screens.project_name(project))} › <b>{warmup.CATEGORIES[name]}</b>: " \
           f"{len(paths)}{split}\n\n"
    hint = f"\n\n{card.CARDS_HINT}" if live else ""
    text = top + fit_lines(lines, MAX_MESSAGE - len(top) - len(hint), len(paths) - len(lines)) + hint
    tail = [[(ZIP, AccountBaseCB(action="zip", value=name, project=project).pack()),
             (card.DELETE, AccountBaseCB(action="del", value=name, project=project).pack())],
            *card.back_to_list(project)]
    await card.offer_cards(callback.message, state, live, text, tail, project, edit=True)


@router.callback_query(MenuCB.filter(F.action == MenuAction.POLL))
async def poll(callback: CallbackQuery, pool: WorkerPool, manager: JobManager):
    """📡: every worker asked for its profile (workers.accounts), as new messages; the screen stays."""
    await callback.answer()
    await workers.accounts(callback.message, pool, manager)


def _zip(directory: str, paths: list[str], fmt: str) -> tuple[bytes, int]:
    """(the ZIP of the accounts' files in `fmt`, how many of them could not be read): each under its path in the
    project's folder (a dead one under inactive/)."""
    buffer, skipped = io.BytesIO(), 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive, tempfile.TemporaryDirectory() as tmp:
        for path in paths:
            name = os.path.relpath(path, directory)
            if fmt == "jsession":  # as the bot keeps it
                archive.write(path, name)
                continue
            into = os.path.join(tmp, os.path.dirname(name))
            os.makedirs(into, exist_ok=True)
            try:
                written = session_import.export(path, into)
            except (OSError, ValueError, KeyError, TypeError):  # a broken file
                skipped += 1
                continue
            for file in written:
                archive.write(file, os.path.relpath(file, tmp))
    return buffer.getvalue(), skipped


@router.callback_query(AccountBaseCB.filter(F.action == "zip"))
async def category_zip(callback: CallbackQuery, callback_data: AccountBaseCB, pool: WorkerPool,
                       workspace: Workspace):
    """📦: its format asked in place first (FORMATS), then the ZIP as a document; the screen stays."""
    name, project = callback_data.value, workspace.project_id
    paths = _paths(pool, name)
    if not paths:
        await callback.answer("Нет файлов для архива.", show_alert=True)
        return
    await callback.answer()
    what = "Все аккаунты" if name == "all" else warmup.CATEGORIES[name]
    if callback_data.fmt not in FORMATS:
        heading = head(WHOLE_BASE) if name == "all" else f"{head(ZIP)} — {what}"
        back = nav_button("✖️ Отмена", ACCOUNT_LIST, project) if name == "all" else \
            _button("✖️ Отмена", "cat", name, project)
        lines = [f"{heading}: файлов {len(paths)} — в каком формате?", "",
                 *(f"<b>{label}</b> — {hint}" for label, hint in FORMATS.values()), "", f"<i>{SAME_KEY}</i>"]
        rows = [[button(label, AccountBaseCB(action="zip", value=name, project=project, fmt=fmt))]
                for fmt, (label, _) in FORMATS.items()]
        await callback.message.edit_text("\n".join(lines), parse_mode="HTML",
                                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[*rows, [back]]))
        return
    data, skipped = await asyncio.to_thread(_zip, pool.storage.directory, paths, callback_data.fmt)
    title = screens.project_name(project) or str(project)
    note = f" Не прочитано: {skipped}." if skipped else ""
    await callback.message.answer_document(
        BufferedInputFile(data, filename=f"{title}_{name}_{callback_data.fmt}.zip"),
        caption=f"📦 {what} · {FORMATS[callback_data.fmt][0].split(' ', 1)[1]}: аккаунтов {len(paths) - skipped}."
                f"{note} Храните архив в надёжном месте. {SAME_KEY}",
    )


@router.callback_query(AccountBaseCB.filter(F.action == "del"))
async def delete_ask(callback: CallbackQuery, callback_data: AccountBaseCB, state: FSMContext, pool: WorkerPool,
                     workspace: Workspace):
    name, project = callback_data.value, workspace.project_id
    # the accounts asked about: the confirmation deletes these only (a warmup may move others into it meanwhile)
    paths = _paths(pool, name)
    if name not in warmup.CATEGORIES or not paths:  # emptied since the category was shown
        await callback.answer(EMPTY_CATEGORY, show_alert=True)
        return
    await callback.answer()
    await state.update_data(base_delete={"name": name, "paths": paths})
    await callback.message.edit_text(  # in the category's place: ✖️ brings it back
        f"{head(card.DELETE)} файлы сессий категории {warmup.CATEGORIES[name]} ({len(paths)})? "
        "Аккаунты из Telegram не выходят, но в проекте их больше не будет. Это необратимо.", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_button("🗑 Да, удалить", "delok", name, project),
                                                            _button("✖️ Отмена", "cat", name, project)]]))


@router.callback_query(AccountBaseCB.filter(F.action == "delok"))
async def delete_run(callback: CallbackQuery, callback_data: AccountBaseCB, state: FSMContext, pool: WorkerPool,
                     workspace: Workspace):
    name = callback_data.value
    asked = (await state.get_data()).get("base_delete") or {}
    if confirm_expired(callback.message) or asked.get("name") != name:  # old, or not the last one asked
        await callback.answer("Подтверждение устарело — откройте категорию заново.", show_alert=True)
        return
    await callback.answer()
    await state.update_data(base_delete=None)
    # the ones asked about, still in the category: none it took in since, none that left it
    deleted, failed = await delete_workers(pool, [path for path in _paths(pool, name) if path in asked["paths"]])
    lines = [f"🗑 {warmup.CATEGORIES.get(name, name)}: удалено {len(deleted)}"]
    lines += [f"• {html.escape(label)} — {html.escape(why)}" for label, why in failed]
    await callback.message.edit_text(fit_lines(lines), parse_mode="HTML",
                                     reply_markup=back_kb(ACCOUNT_LIST, workspace.project_id, fresh=False))
