import asyncio
import html
import io
import os
import secrets
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import ChoiceCB, MenuAction, MenuCB
from bot.keyboards.common import cancel_kb, choice_kb, marks_kb, paged_kb
from bot.keyboards.menu import main_menu
from bot.routers._common import (STALE_BUTTON, delete_quietly, ensure_workers, marks_step, proxies_busy,
                                 rehome_now, send_chunked)
from bot.services import login as logins
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from bot.services.workspaces import Workspace, Workspaces
from bot.services import screens
from modules.registry import ACCOUNTS, MAIN_MENU, menu_path
from bot.states import ImportTdata, MoveWorkers, SetProxy
from functions.spamblock import set_restricted_proxies
from modules import login as sign_in, phone_countries, projects, restricted_workers, scraped_files, tdata_import
from modules.storages.sessions_storage import release_client
from modules.scraper_creds import card_lines, find_account, scrape_accounts
from modules.storages.sessions_storage import profile_order
from modules.types.proxy import ACCOUNTS_PER_PROXY, overlap, parse_proxies

router = Router()

GET_ME_TIMEOUT = 20  # seconds per worker when polling profiles for the accounts list

MAX_PROXY_FILE_SIZE = 256 * 1024       # a proxy list is tiny; reject anything clearly wrong
MAX_TDATA_ZIP_SIZE = 10 * 1024 * 1024  # a tdata archive is small; cap at 10 MB
MAX_TDATA_UNZIPPED = 100 * 1024 * 1024  # and so is what it unpacks to: a 10 MB archive mustn't fill the disk

NO_WORKERS = (f"Добавьте аккаунт ({menu_path('add')}) — по номеру, из tdata или из свободных, — "
              "чтобы запускать задачи проекта.")
STALE_LIST = f"Список устарел — откройте заново: {menu_path('list')}."

SPARE_PROMPT = (
    "Прокси для <b>бессрочно ограниченных</b> воркеров — необязательно. Они уйдут с основных прокси "
    "и освободят места для новых аккаунтов, а сами продолжат отвечать на входящие. Лимита на прокси "
    "нет: хватит одного-двух. Пришлите список так же или нажмите ⏭ Пропустить."
)

PROXY_PROMPT = (
    "Пришлите список прокси — <b>.txt файлом</b> или текстом, по одному на строку:\n"
    "<code>scheme://user:pass@ip:port</code> (socks5/socks4/http).\n\n"
    f"Они распределятся по аккаунтам проекта: один прокси на {ACCOUNTS_PER_PROXY} аккаунта. Новые аккаунты "
    "проекта тоже получат прокси из этого списка."
)


def _worker_line(storage, client, me, index: int, busy: bool = False, mark: str = "") -> str:
    """One account's card for the list; falls back to stored data when get_me() failed
    or was not asked (busy: the scraper runs on it)."""
    if me is None:
        status = "⏳ занят скрапом" if busy else "⚠️ не удалось опросить"
        js = storage.jsessions_paths.get(storage.get_session_path(client))
        if js is not None:
            account = js.account.account
            name = " ".join(filter(None, [account.first_name, account.last_name])) or "—"
            return (f"<b>{index}. {html.escape(name)}</b>\n"
                    f"📱 <code>+{html.escape(str(account.phone_number))}</code> · "
                    f"🆔 <code>{account.user_id}</code>\n"
                    f"{status}{mark}")
        return f"<b>{index}.</b> {status} (StringSession){mark}"

    name = " ".join(filter(None, [me.first_name, me.last_name])) or "—"
    username = f"@{html.escape(me.username)}" if me.username else "—"  # plain: Telegram links it
    premium = " · ⭐ Premium" if me.premium else ""
    return (f"<b>{index}. {html.escape(name)}</b>\n"
            f"👤 {username} · 🆔 <code>{me.id}</code>{premium}{mark}")


@screens.view(ACCOUNTS)
async def accounts_screen(message: Message, state: FSMContext, edit: bool, pool: WorkerPool, **_):
    header = f"🤖 <b>Аккаунты проекта</b> — воркеров: <b>{pool.count()}</b>"
    if pool.count() == 0:
        header += f"\n\n{NO_WORKERS}"
    await screens.show(message, ACCOUNTS, edit, header)


@router.callback_query(MenuCB.filter(F.action == MenuAction.LIST))
async def list_accounts(callback: CallbackQuery, state: FSMContext, pool: WorkerPool, manager: JobManager, personal):
    await callback.answer()
    await accounts(callback.message, state, pool, manager, personal)


async def accounts(message: Message, state: FSMContext, pool: WorkerPool, manager: JobManager, personal):
    order = await _list_workers(message, pool, manager)
    await _list_personal(message, state, pool, personal, order)


def _countries(storage, workers, profiles) -> str:
    """The workers' countries, a line under the list's header; "" with no number known."""
    countries = phone_countries.summary(phone_countries.phone_of(storage, client, me)
                                        for client, me in zip(workers, profiles))
    return f"\n🌍 {html.escape(countries)}" if countries else ""


async def _list_workers(message: Message, pool: WorkerPool, manager: JobManager) -> list[str]:
    """The workers' list; their session paths in its order ([] when it wasn't polled)."""
    count = pool.count()

    if count == 0:
        await message.answer(
            "👥 <b>Аккаунты</b>\n\n"
            f"🤖 Воркеров: <b>0</b>\n\n{NO_WORKERS}",
            parse_mode="HTML",
        )
        return []

    if not manager.acquire("Просмотр аккаунтов", cancelable=False, timeout=0):
        workers = pool.workers  # their stored numbers only: none is polled now
        await message.answer(
            f"👥 <b>Аккаунты</b> — всего: <b>{count}</b>{_countries(pool.storage, workers, [None] * len(workers))}"
            f"\n\n⛔ Идёт задача «{manager.label}». Детали аккаунтов — после её завершения.",
            parse_mode="HTML",
        )
        return []

    # the scraper's worker isn't polled: its auth key is in use by the scrape's own client, and
    # a second connection from another IP (a rotating proxy) risks AUTH_KEY_DUPLICATED
    scraping = pool.scraping.path if pool.scraping is not None else None

    def busy(client) -> bool:
        return scraping is not None and pool.storage.get_session_path(client) == scraping

    async def profile(client):
        return None if busy(client) else await pool.storage.fetch_me(client, GET_ME_TIMEOUT)

    try:
        workers = pool.workers
        pool.in_job = [w for w in workers if not busy(w)]  # polled now: no scrape starts on them
        profiles = await asyncio.gather(*[profile(client) for client in workers])
    finally:
        pool.in_job = []
        manager.release()

    forever, status = set(restricted_workers.load()), restricted_workers.load_status()
    # the last @SpamBot check: (group, label); restricted ones go last
    checks = {id(client): restricted_workers.classify(pool.storage.get_session_path(client), forever, status)
              for client in workers}

    ordered = sorted(zip(workers, profiles), key=lambda pair: (checks[id(pair[0])][0], profile_order(pair[1])))
    lines = [_worker_line(pool.storage, client, me, i + 1, busy(client), "\n" + html.escape(checks[id(client)][1]))
             for i, (client, me) in enumerate(ordered)]

    header = f"👥 <b>Аккаунты</b> — всего: <b>{count}</b>"
    if restricted := sum(1 for group, _ in checks.values() if group):
        header += f" · ограничены: <b>{restricted}</b>"
    header += _countries(pool.storage, workers, profiles)
    await send_chunked(message, header, lines)
    return [pool.storage.get_session_path(client) for client, _ in ordered]


# --- personal accounts (personal_sessions/), each account's card ---------------------------

async def _list_personal(message: Message, state: FSMContext, pool: WorkerPool, personal, order=()):
    """The personal accounts, as stored: one is never connected just to be listed; under them a
    button for each account's card, as listed: the workers in `order` (their list's), then the personal ones."""
    rank = {path: i for i, path in enumerate(order)}
    listed = sorted(scrape_accounts(pool.storage, personal),
                    key=lambda account: (account.personal, rank.get(account.path, len(rank))))
    if not listed:
        return
    # the buttons carry an index into acc_paths and this list's token: an older list's (or card's)
    # must not act on the account at its index now
    token = secrets.token_hex(4)
    labels = [f"{'👤' if account.personal else '🤖'} {account.label}" for account in listed]
    await state.update_data(acc_paths=[account.path for account in listed], acc_labels=labels,
                            acc_token=token)
    text = "🗂 Карточка аккаунта — код входа, удаление личного:"
    if own := [account for account in listed if account.personal]:
        lines = "\n".join(f"{i}. {html.escape(account.label)}" for i, account in enumerate(own, 1))
        text = f"👤 <b>Личные</b> (только для скрапа): <b>{len(own)}</b>\n\n{lines}\n\n{text}"
    await message.answer(text, parse_mode="HTML", reply_markup=_cards_kb(labels, token, 0))


def _cards_kb(labels: list[str], token: str, page: int):
    """The list's buttons, a page of them (keyboards.paged_kb), each with the list's token."""
    return paged_kb("acc", [(label, f"{i}.{token}") for i, label in enumerate(labels)], page, f".{token}")


async def _account_at(state: FSMContext, pool: WorkerPool, personal, key: str):
    """The account of a button's key ("index.token"); None for a stale one (another list since, or a flow
    cleared it)."""
    index, token = key.split(".")
    data = await state.get_data()
    paths, index = data.get("acc_paths", []), int(index)
    if token != data.get("acc_token") or not 0 <= index < len(paths):
        return None
    return find_account(scrape_accounts(pool.storage, personal), paths[index])


def _card(pool: WorkerPool, personal, account, key: str):
    """(text, keyboard) of an account's card: from what is stored, at once — never connected for it."""
    storage = personal if account.personal else pool.storage
    lines = card_lines(storage, account, html=True)
    lines[0] = f"<b>{lines[0]}</b>"
    if pool.scraped(account.path):
        lines.append("⏳ занят скрапом")
    actions = [("🔑 Код входа", f"codes.{key}")]
    if account.personal:
        actions.append(("🗑 Убрать", f"rm.{key}"))
    return "\n".join(lines), choice_kb("acc_act", actions, widths=(2,))


def _codes_text(account, codes, now: datetime) -> str:
    lines = [f"🔑 <b>Коды входа</b> — {html.escape(account.label)}", ""]
    if not codes:
        lines.append(f"{sign_in.NO_CODES} и нажмите 🔄 Обновить.")
    for code, sent_at in codes:
        lines.append(f"<code>{sign_in.spaced(code)}</code> · {sign_in.ago(sent_at, now)}")
    return "\n".join(lines)


async def _codes(pool: WorkerPool, account, key: str) -> tuple[str, object]:
    """(text, keyboard) of an account's (a worker's or a personal one's) recent login codes, in its card's place."""
    keyboard = choice_kb("acc_act", [("🔄 Обновить", f"codes.{key}"), ("⬅️ Назад", f"card.{key}")], widths=(2,))
    if pool.scraped(account.path):
        # its key is in use by the scrape's own client: a second connection risks AUTH_KEY_DUPLICATED
        return f"⏳ {html.escape(account.label)} занят скрапом — коды после его окончания.", keyboard
    try:
        codes = await logins.fetch_codes(pool, account.client, account.path)
    except Exception as err:  # a dead proxy, a logged-out session: a timeout has no text
        why = html.escape(str(err) or type(err).__name__)
        return f"⚠️ {html.escape(account.label)}: не удалось подключиться ({why}).", keyboard
    except asyncio.CancelledError:
        if asyncio.current_task().cancelling():  # the bot is stopping
            raise
        # a job's end released this account mid-read: Telethon cancels a disconnected client's requests
        return f"⚠️ {html.escape(account.label)}: соединение прервано — нажмите 🔄.", keyboard
    return _codes_text(account, codes, datetime.now(timezone.utc)), keyboard


@router.callback_query(ChoiceCB.filter(F.scope == "acc"))
async def account_card(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext, pool: WorkerPool,
                       personal):
    await callback.answer()
    if callback_data.value.startswith("pg."):  # ⬅️ · 📄 · ➡️: another page of the list's buttons
        _, page, token = callback_data.value.split(".")
        data = await state.get_data()
        if token != data.get("acc_token"):
            await callback.message.answer(STALE_LIST)
            return
        try:
            await callback.message.edit_reply_markup(reply_markup=_cards_kb(data["acc_labels"], token, int(page)))
        except Exception:  # 📄, the page shown: "message is not modified"
            pass
        return
    account = await _account_at(state, pool, personal, callback_data.value)
    if account is None:
        await callback.message.answer(STALE_LIST)
        return
    text, markup = _card(pool, personal, account, callback_data.value)
    await callback.message.answer(text, parse_mode="HTML", reply_markup=markup)


@router.callback_query(ChoiceCB.filter(F.scope == "acc_act"))
async def account_action(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext, pool: WorkerPool,
                         personal):
    """A card's button: 🔑 (and its 🔄) and ⬅️ in the card's place, 🗑 asks first."""
    await callback.answer()
    action, key = callback_data.value.split(".", 1)
    account = await _account_at(state, pool, personal, key)
    if account is None or (action == "rm" and not account.personal):
        await callback.message.answer(STALE_LIST)
        return
    if action == "rm":
        await _confirm_removal(callback.message, state, account, key)
        return
    text, markup = _card(pool, personal, account, key) if action == "card" else await _codes(pool, account, key)
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    except Exception:  # 🔄 with nothing new: "message is not modified"
        pass


async def _confirm_removal(message: Message, state: FSMContext, account, key: str):
    # the button carries the card's key; prm_confirm, this account: it removes this account only
    await state.update_data(prm_confirm=account.path)
    await message.answer(
        f"Убрать личный аккаунт {html.escape(account.label)}?\n\n"
        f"Бот завершит авторизацию из этого файла и удалит его: <code>{html.escape(account.path)}</code>. "
        "Вход на телефоне не затрагивается; но если файл получен из tdata Telegram Desktop, "
        "тот Desktop тоже выйдет из аккаунта.",
        parse_mode="HTML",
        reply_markup=choice_kb("prm_ok", [("🗑 Да, убрать", key), ("↩️ Отмена", "no")]),
    )


def _removal_blocked(pool: WorkerPool, path: str) -> str | None:
    """Why the personal account can't be removed now, or None."""
    if pool.scraped(path):
        return "⛔ На этом аккаунте идёт скрап — уберите его после окончания."
    if pool.busy(path):
        return ("⛔ Бот сейчас читает коды входа или проверяет названия чатов на этом аккаунте — "
                "повторите через несколько секунд.")
    if scraped_files.unfinished_scrape(path):
        return "⛔ На этом аккаунте есть незавершённый скрап — продолжить его можно только на нём."
    return None


# --- workers moved by ticks: free ones into the project, the project's out of it ----------------
# The lists of the worker picker (profile.py, keyboards.marks_kb): ✅ / ⬜, ☑️ Все, ⬜ Никого, then ▶️.

TICKS = [("☑️ Все", "all"), ("⬜ Никого", "none")]
FREE_HEAD = "📦 <b>Свободные аккаунты</b> — ещё ни в одном проекте. Отмеченные станут воркерами этого проекта."
OUT_HEAD = ("↪️ <b>Перенести воркеров</b> — в другой проект или в свободные. Ограничения от @SpamBot и прокси "
            "аккаунты унесут с собой; люди, добавленные ими в контакты, в этом проекте перейдут к другим воркерам.")


def _ticks_text(head: str, picked: list[int], total: int) -> str:
    return f"{head}\n\nОтмечено: {len(picked)} из {total}"


async def _ask_ticks(message: Message, state: FSMContext, step, head: str, scope: str, paths: list[str],
                     labels: list[str], go: str):
    await state.clear()  # an abandoned flow's data must not reach this one
    await state.set_state(step)
    await state.update_data(move_paths=paths, move_labels=labels, picked=[], move_page=0)
    await message.answer(_ticks_text(head, [], len(paths)), parse_mode="HTML",
                         reply_markup=marks_kb(scope, labels, [], 0, TICKS + [(go, "go")], (2, 1)))


async def _ticked(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext, head: str, scope: str,
                  go: str) -> list[str] | None:
    """A tap on a ticks list: the list redrawn; on ▶️, the ticked paths (None till then)."""
    data = await state.get_data()
    paths, labels, picked, page = data["move_paths"], data["move_labels"], data["picked"], data["move_page"]
    if callback_data.value == "go":
        if not picked:
            await callback.answer("Отметьте хотя бы один аккаунт", show_alert=True)
            return None
        await callback.answer()
        return [paths[index] for index in picked]
    await callback.answer()
    new_picked, new_page = marks_step(callback_data.value, picked, page, len(paths),
                                      {"all": range(len(paths)), "none": []})
    if (new_picked, new_page) != (picked, page):  # an unchanged message can't be edited (📄 too)
        await state.update_data(picked=new_picked, move_page=new_page)
        await callback.message.edit_text(_ticks_text(head, new_picked, len(paths)), parse_mode="HTML",
                                         reply_markup=marks_kb(scope, labels, new_picked, new_page,
                                                               TICKS + [(go, "go")], (2, 1)))
    return None


def _moved_text(done: str, moved: list[str], failed: list[tuple[str, str]], total: int) -> str:
    """The summary of a move: how many of the ticked, then each one not moved and why."""
    lines = [f"{done}: {len(moved)} из {total}."]
    if failed:
        lines += ["", "Не перенесены:", *(f"• {html.escape(label)} — {html.escape(why)}" for label, why in failed)]
    return "\n".join(lines)


@router.callback_query(MenuCB.filter(F.action == MenuAction.FREE))
async def free_start(callback: CallbackQuery, state: FSMContext):
    """📦 Из свободных (➕ Добавить): the accounts in no project, read offline, to tick."""
    await callback.answer()
    free = [path for path in projects.account_files(projects.FREE_DIR) if path.endswith(".jsession")]
    if not free:
        await callback.message.answer(
            f"Свободных аккаунтов нет — все уже в проектах. (Свободные — файлы .jsession в "
            f"<code>{projects.FREE_DIR}/</code>.)", parse_mode="HTML")
        return
    await _ask_ticks(callback.message, state, MoveWorkers.free, FREE_HEAD, "mv_free", free,
                     [projects.account_label(path) for path in free], "▶️ Добавить в проект")


@router.callback_query(MoveWorkers.free, ChoiceCB.filter(F.scope == "mv_free"))
async def free_pick(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext, pool: WorkerPool):
    paths = await _ticked(callback, callback_data, state, FREE_HEAD, "mv_free", "▶️ Добавить в проект")
    if paths is None:
        return
    await state.clear()
    moved, failed = [], []
    for path in paths:
        label = projects.account_label(path) if os.path.exists(path) else os.path.basename(path)
        if not os.path.exists(path):  # taken meanwhile, from another chat
            failed.append((label, "его уже нет среди свободных"))
            continue
        try:
            pool.storage.add_jsession(projects.move_account(path, pool.storage.directory))
        except (ValueError, OSError) as err:
            failed.append((label, str(err)))
            continue
        moved.append(label)
    await callback.message.edit_text(
        _moved_text("✅ Добавлено в проект", moved, failed, len(paths)) + f"\n🤖 Воркеров в проекте: {pool.count()}",
        parse_mode="HTML")


@router.callback_query(MenuCB.filter(F.action == MenuAction.MOVE))
async def move_start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool):
    """↪️ Перенести: the project's workers to tick; one in use now is marked, and stays."""
    await callback.answer()
    if not await ensure_workers(callback, pool):
        return
    # a StringSession has nothing to load it by elsewhere: moved by hand
    workers = [account for account in scrape_accounts(pool.storage) if account.path.endswith(".jsession")]
    labels = [f"{account.label}{' ⏳ занят' if pool.busy(account.path) or pool.scraped(account.path) else ''}"
              for account in workers]
    await _ask_ticks(callback.message, state, MoveWorkers.out, OUT_HEAD, "mv_out",
                     [account.path for account in workers], labels, "▶️ Далее")


@router.callback_query(MoveWorkers.out, ChoiceCB.filter(F.scope == "mv_out"))
async def move_pick(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext, workspace: Workspace):
    paths = await _ticked(callback, callback_data, state, OUT_HEAD, "mv_out", "▶️ Далее")
    if paths is None:
        return
    try:
        others = [project for project in projects.load() if project.id != workspace.project_id]
    except ValueError as err:
        await state.clear()
        await callback.message.answer(f"⚠️ {err}")
        return
    await state.set_state(MoveWorkers.target)
    await state.update_data(move_chosen=paths)
    await callback.message.edit_text(
        f"Куда перенести отмеченных — {len(paths)}?",
        reply_markup=choice_kb("mv_to", [*((f"📁 {project.name}", str(project.id)) for project in others),
                                         ("📦 В свободные", "free")]))


@router.callback_query(MoveWorkers.target, ChoiceCB.filter(F.scope == "mv_to"))
async def move_to(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext, pool: WorkerPool,
                  workspaces: Workspaces):
    await callback.answer()
    paths = (await state.get_data())["move_chosen"]
    await state.clear()
    try:
        project = None if callback_data.value == "free" else next(
            p for p in projects.load() if str(p.id) == callback_data.value)
    except (ValueError, StopIteration):  # an unreadable file, or the project deleted meanwhile
        await callback.message.answer("Этого проекта уже нет — начните заново.")
        return
    target = None if project is None else workspaces.get(project.id)  # else it reads its folder when opened
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
    where = "в свободные" if project is None else f"в проект «{project.name}»"
    await callback.message.edit_text(_moved_text(f"✅ Перенесено {html.escape(where)}", moved, failed, len(paths)),
                                     parse_mode="HTML")


@router.callback_query(ChoiceCB.filter(F.scope.in_({"mv_free", "mv_out", "mv_to"})))
async def move_stale(callback: CallbackQuery):
    """A ticks list's button whose flow is gone (another one began, or a restart)."""
    await callback.answer(STALE_BUTTON)


@router.callback_query(ChoiceCB.filter(F.scope == "prm_ok"))
async def personal_remove(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext,
                          pool: WorkerPool, personal):
    await callback.answer()
    if callback_data.value == "no":
        await state.clear()
        await callback.message.answer("Отменено.")
        return
    account = await _account_at(state, pool, personal, callback_data.value)
    if account is None or not account.personal or account.path != (await state.get_data()).get("prm_confirm"):
        await callback.message.answer(STALE_LIST)
        return
    if reason := _removal_blocked(pool, account.path):
        await callback.message.answer(reason)
        return

    await state.clear()
    logged_out = await sign_in.remove_personal(personal, account)
    label = html.escape(account.label)
    await callback.message.answer(
        f"✅ Личный аккаунт {label} убран: сессия бота завершена, файл удалён." if logged_out else
        f"⚠️ Файл личного аккаунта {label} удалён, но завершить сессию бота не удалось — "
        "завершите её вручную: Telegram → Настройки → Устройства.",
        reply_markup=main_menu(),
    )


@router.callback_query(MenuCB.filter(F.action == MenuAction.PROXY))
async def proxy_start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool, manager: JobManager):
    await callback.answer()

    if not await ensure_workers(callback, pool):
        return

    if manager.active:
        await callback.message.answer(f"⛔ Занят: {manager.label}. Дождитесь завершения задачи.")
        return

    await state.set_state(SetProxy.input)
    await callback.message.answer(PROXY_PROMPT, parse_mode="HTML", reply_markup=cancel_kb(SetProxy.input))


async def _proxy_list(message: Message):
    """The proxies a message holds (a .txt or text) and its raw text; None once the problem is told."""
    if message.document is not None:
        if (message.document.file_size or 0) > MAX_PROXY_FILE_SIZE:
            await message.answer("Файл слишком большой для списка прокси. Пришлите .txt поменьше.")
            return None
        buffer = await message.bot.download(message.document)
        raw = buffer.read().decode("utf-8", "replace")
    else:
        raw = message.text or ""

    try:
        proxies = parse_proxies(raw)
    except ValueError as err:
        await message.answer(f"Не удалось разобрать прокси: {err}. Исправьте и пришлите снова.")
        return None

    if not proxies:
        await message.answer("Пустой список. Пришлите хотя бы один прокси.")
        return None
    return proxies, raw


@router.message(SetProxy.input, ~F.text.in_(MAIN_MENU))
async def proxy_apply(message: Message, state: FSMContext, pool: WorkerPool, manager: JobManager):
    if (got := await _proxy_list(message)) is None:
        return
    proxies, raw = got
    try:  # the restricted workers' own proxies, set before: they stay off this list (see SPARE_PROMPT)
        spare = tdata_import.load_proxies(projects.restricted_proxies_path(pool.storage.home))
    except ValueError as err:  # edited badly: that list first (its step), or the pool's could fall short
        await state.set_state(SetProxy.spare)
        await message.answer(
            f"⚠️ Список прокси для ограниченных испорчен ({err}). Сначала пришлите его заново или нажмите "
            "«🗑 Не отделять», затем снова 🌐 Прокси с основным списком.",
            reply_markup=cancel_kb(SetProxy.spare, choice_kb("proxy_spare", [("🗑 Не отделять", "clear")])))
        return
    if overlap(proxies, spare):
        await message.answer("⚠️ В списке есть прокси для бессрочно ограниченных. Уберите их — или сначала "
                             "отключите отделение: 🌐 Прокси без них, затем «🗑 Не отделять».")
        return
    if await proxies_busy(message, pool, manager):
        return

    try:
        summary = pool.storage.apply_proxies(proxies, spare, set(restricted_workers.load()))
    except ValueError as err:
        await message.answer(f"⚠️ {err}. Пришлите больше прокси.")
        return

    tdata_import.save_proxies(raw, projects.proxies_path(pool.storage.home))

    await state.set_state(SetProxy.spare)

    text = (
        f"✅ Прокси применены к <b>{summary['accounts']}</b> аккаунтам "
        f"(использовано прокси: {summary['proxies_used']})."
    )
    if summary["restricted"]:
        text += f"\nБессрочно ограниченных на своих прокси: {summary['restricted']}."
    if summary["string_sessions_skipped"]:
        text += f"\n⚠️ Пропущено .session-аккаунтов (без метаданных): {summary['string_sessions_skipped']}."

    await message.answer(text, parse_mode="HTML")
    # 🗑 for a list there, readable or not: a broken one can be dropped too
    aside = os.path.exists(projects.restricted_proxies_path(pool.storage.home))
    buttons = [("⏭ Пропустить", "skip")] + ([("🗑 Не отделять", "clear")] if aside else [])
    now = f"\nСейчас задано: {len(spare)}." if spare else ""
    await message.answer(SPARE_PROMPT + now, parse_mode="HTML",
                         reply_markup=cancel_kb(SetProxy.spare, choice_kb("proxy_spare", buttons)))


@router.callback_query(SetProxy.spare, ChoiceCB.filter(F.scope == "proxy_spare"))
async def proxy_spare_choice(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext,
                             pool: WorkerPool, manager: JobManager):
    await callback.answer()
    if callback_data.value == "skip":
        await state.clear()
        await callback.message.answer("Готово: прокси для ограниченных — без изменений.", reply_markup=main_menu())
        return

    # 🗑 Не отделять: the list goes, the ones on it back into the pool
    if await proxies_busy(callback.message, pool, manager):
        return
    set_restricted_proxies(pool.storage, None)
    await state.clear()
    report = await rehome_now(callback.message, pool)
    await callback.message.answer("🗑 Ограниченные больше не отделяются." + (f"\n{report}" if report else ""),
                                  reply_markup=main_menu())


@router.message(SetProxy.spare, ~F.text.in_(MAIN_MENU))
async def proxy_spare_apply(message: Message, state: FSMContext, pool: WorkerPool, manager: JobManager):
    if (got := await _proxy_list(message)) is None:
        return
    spare, raw = got
    try:
        main = tdata_import.load_proxies(projects.proxies_path(pool.storage.home))
    except ValueError as err:
        await message.answer(f"⚠️ Основной список прокси испорчен ({err}): пришлите его заново — 🌐 Прокси.")
        return
    if overlap(spare, main):
        await message.answer("⚠️ Эти прокси уже в основном списке: для ограниченных нужны другие.")
        return
    if await proxies_busy(message, pool, manager):
        return
    set_restricted_proxies(pool.storage, raw)
    await state.clear()
    report = await rehome_now(message, pool)
    await message.answer("✅ Прокси для ограниченных сохранены." + (f"\n{report}" if report else ""),
                         reply_markup=main_menu())


# --- tdata upload ---------------------------------------------------------

@router.callback_query(MenuCB.filter(F.action == MenuAction.TDATA))
async def tdata_start(callback: CallbackQuery, state: FSMContext, manager: JobManager):
    await callback.answer()

    if manager.active:
        await callback.message.answer(f"⛔ Занят: {manager.label}. Дождитесь завершения задачи.")
        return

    await state.set_state(ImportTdata.archive)
    await callback.message.answer(
        "Пришлите <b>ZIP-архив с одной папкой tdata</b> (Telegram Desktop).",
        parse_mode="HTML", reply_markup=cancel_kb(ImportTdata.archive),
    )


@router.message(ImportTdata.archive, ~F.text.in_(MAIN_MENU))
async def tdata_archive(message: Message, state: FSMContext):
    document = message.document

    if document is None:
        await message.answer("Ожидается ZIP-файл. Пришлите архив с папкой tdata.")
        return

    if (document.file_size or 0) > MAX_TDATA_ZIP_SIZE:
        await message.answer("Архив слишком большой. tdata весит немного — пришлите ZIP поменьше.")
        return

    buffer = await message.bot.download(document)
    data = buffer.read()

    if not zipfile.is_zipfile(io.BytesIO(data)):
        await message.answer("Это не ZIP-архив. Пришлите папку tdata, упакованную в .zip.")
        return

    await state.update_data(zip=data)
    await state.set_state(ImportTdata.password)
    await message.answer("Введите пароль 2FA для аккаунта («-», если пароля нет):",
                         reply_markup=cancel_kb(ImportTdata.password))


@router.message(ImportTdata.password, ~F.text.in_(MAIN_MENU))
async def tdata_password(message: Message, state: FSMContext, pool: WorkerPool, manager: JobManager):
    raw = message.text  # as typed: spaces may be part of it, as at the sign-in by phone
    if not raw:  # a sticker/photo must not import with "no password"
        await message.answer("Ожидается текст. Попробуйте ещё раз.")
        return
    password = None if raw.strip() == "-" else raw
    await delete_quietly(message)

    data = await state.get_data()
    zip_bytes = data.get("zip")
    if not zip_bytes:
        await state.clear()
        await message.answer("Архив потерялся, начните заново.", reply_markup=main_menu())
        return

    if not manager.acquire("Импорт tdata", cancelable=False, timeout=0):
        await message.answer(f"⛔ Занят: {manager.label}. Дождитесь завершения задачи.")
        return

    await state.clear()
    tmp_dir = None

    try:
        tmp_dir = tempfile.mkdtemp(prefix="tdata_")  # inside try: a full disk must not hold the slot
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            if sum(info.file_size for info in archive.infolist()) > MAX_TDATA_UNZIPPED:
                await message.answer("Архив слишком велик в распакованном виде для папки tdata.",
                                     reply_markup=main_menu())
                return
            archive.extractall(tmp_dir)

        tdata_dir = tdata_import.find_tdata_dir(tmp_dir)
        if tdata_dir is None:
            await message.answer("В архиве не найдена папка tdata.", reply_markup=main_menu())
            return

        used = [js.account.proxy for js in pool.storage.jsessions_paths.values()]
        try:
            proxy = tdata_import.choose_pool_proxy(
                used, tdata_import.load_proxies(projects.proxies_path(pool.storage.home)))
        except ValueError as err:
            await message.answer(f"⚠️ {err}.", reply_markup=main_menu())
            return

        await message.answer("Импортирую аккаунт…")
        phone = await tdata_import.convert(tdata_dir, proxy, password, pool.storage.directory)

        if phone is None:
            await message.answer("⚠️ Аккаунт не авторизован — импорт отменён.", reply_markup=main_menu())
            return

        # an account already loaded may sit in a file not named after its phone
        session = (None if pool.storage.is_phone_exists(phone)
                   else pool.storage.add_jsession(os.path.join(pool.storage.directory, f"{phone}.jsession")))
        if session is None:
            await message.answer(
                f"ℹ️ Аккаунт +{phone} уже есть в проекте — файл не изменён.",
                reply_markup=main_menu(),
            )
            return

        # read back from the file: if it was already on disk, convert() kept its settings
        saved = session.account
        proxy_note = saved.proxy.label() if saved.proxy else "без прокси"
        await message.answer(
            f"✅ Аккаунт импортирован.\n"
            f"📱 Номер: +{phone}\n"
            f"🔐 2FA: {'да' if saved.password else 'нет'}\n"
            f"🌐 Прокси: {proxy_note}\n"
            f"🤖 Воркеров в проекте: {pool.count()}",
            reply_markup=main_menu(),
        )
    except Exception as err:
        await message.answer(f"⚠️ Ошибка импорта: {err}", reply_markup=main_menu())
    finally:
        manager.release()
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)
