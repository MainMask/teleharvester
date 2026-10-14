"""What the scraper's jobs in the bot share — a scrape (scrape.py), a group's members (members.py), a
verify (verify.py): the account a job runs on (a worker or a personal account, personal_sessions/,
never a worker), with its own client, in its own job slot (`scrapes`), so it never drives the worker
pool nor holds up the other tasks; the check of the typed chats' names on that account; the slot with
its ⏹; the result files sent back. The analysis of the files (analysis.py) has a slot of its own."""
import asyncio
import os

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message

from bot.callbacks import ChoiceCB
from bot.keyboards.common import cancel_kb, choice_kb, paged_kb
from bot.routers._common import STALE_BUTTON, menu_answer, send_chunked
from bot.services import scraping
from bot.services.delegation import WorkerPool, borrow
from bot.services.jobs import JobManager
from bot.services.progress import Progress
from modules import projects, scraped_files
from modules.registry import menu_path
from modules.scraper_creds import find_account, scrape_accounts
from scraper.scrape import (
    CHAT_LOOKUP_TIMEOUT, chat_block, chat_label,
    lookup_on, parse_date,
)

router = Router()

# the step each flow goes on to once its account is chosen (see choose_account); filled by the flows
FLOW_NEXT = {}
# the step a flow goes on to from its chat list's ▶️ (see check_chats); filled by the flows, called as FLOW_NEXT's
CHATS_NEXT = {}
# the bot is stopping (scrape.stop_for_shutdown): a job stopped now is not the operator's ⏹
shutting_down = False


DEFAULT_OUT = scraped_files.BASES_DIR


async def drop_button(status):
    """The job is over: left in place, its 📊 button would show whatever job runs next."""
    if status is None:
        return
    try:
        await status.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


async def send_files(bot, chat_id, paths):
    for path in paths:
        if not os.path.isfile(path):
            continue  # scrape leaves a <name>_partial/ dir in out_dir; never send a directory
        try:
            if os.path.getsize(path) <= scraping.TELEGRAM_UPLOAD_LIMIT:
                await bot.send_document(chat_id, FSInputFile(path))
            else:
                await bot.send_message(chat_id, f"Файл слишком большой для Telegram, лежит на диске: {path}")
        except Exception as err:  # a failed upload must not silently drop the remaining files
            try:
                await bot.send_message(
                    chat_id, f"Не удалось отправить {os.path.basename(path)}: {err}. Файл на диске: {path}"
                )
            except Exception:
                pass


# A scraper job (scrape / members / verify) runs on one account: a worker or a personal
# account (personal_sessions/, never a worker), with its own client, in its own job slot
# (`scrapes`): it never drives the worker pool, so it doesn't hold up the other tasks.

PERSONAL_WARNING = ("⚠️ Это ваш личный аккаунт. Скрапер только читает, но большой скрап "
                    "с реакциями может вызвать временные лимиты Telegram на этом аккаунте.")


# a base scraped by an account holds access hashes valid for that account only: the workers
# reach its people by username alone (the mailing skips the rest as "another account's base")
BASE_NOTE = ("\n\nБаза людей с этого аккаунта для рассылки через воркеров дойдёт только до тех, "
             "у кого есть username: access hash действует только для собравшего аккаунта. "
             "Для полной базы выберите воркера.")


_BASE_FLOWS = {"scrape_resume", "project", "topup", "members"}  # the flows that build a base


# a worker in a running bot job: one account must not scrape and mail at once (see WorkerPool)
WORKER_BUSY = ("⛔ Этот воркер сейчас занят задачей бота. Выберите другой аккаунт (например, личный) "
               "или дождитесь её окончания.")


PERSONAL_BUSY = ("⛔ Бот сейчас читает коды входа или проверяет названия чатов на этом аккаунте — "
                 "повторите через несколько секунд.")


def _accounts(pool: WorkerPool, personal) -> list:
    return scrape_accounts(pool.storage, personal)


async def choose_account(message: Message, state: FSMContext, flow: str, **deps):
    """Ask which account the flow runs on; with one candidate it is taken at once.
    deps (pool, personal, scrapes) reach the flow's next step."""
    accounts = _accounts(deps["pool"], deps["personal"])
    if not accounts:
        await state.clear()
        await menu_answer(message, f"Нет аккаунтов: добавьте воркера или личный аккаунт — {menu_path('add')}.")
        return
    if len(accounts) == 1:
        await _account_chosen(message, state, accounts[0], flow, deps)
        return
    labels = [f"{'👤' if a.personal else '🤖'} {a.label}" for a in accounts]
    await state.update_data(account_flow=flow, account_paths=[a.path for a in accounts], account_labels=labels)
    await message.answer("На каком аккаунте?", reply_markup=_accounts_kb(labels, 0))


def _accounts_kb(labels: list[str], page: int):
    """The accounts to pick, a page of them (keyboards.paged_kb)."""
    return paged_kb("scr_acc", [(label, str(i)) for i, label in enumerate(labels)], page)


@router.callback_query(ChoiceCB.filter(F.scope == "scr_acc"))
async def account_pick(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext,
                       pool: WorkerPool, personal, scrapes: JobManager):
    await callback.answer()
    data = await state.get_data()
    if callback_data.value.startswith("pg.") and "account_flow" in data:  # ⬅️ · 📄 · ➡️: another page
        try:
            await callback.message.edit_reply_markup(
                reply_markup=_accounts_kb(data["account_labels"], int(callback_data.value[3:])))
        except Exception:  # 📄, the page shown: "message is not modified"
            pass
        return
    paths = data.get("account_paths", [])
    index = int(callback_data.value) if callback_data.value.isdigit() else -1  # pg. of a flow gone: stale
    account = find_account(_accounts(pool, personal), paths[index]) if 0 <= index < len(paths) else None
    if account is None or "account_flow" not in data:  # a stale button
        await callback.message.answer(STALE_BUTTON)
        return
    await _account_chosen(callback.message, state, account, data["account_flow"],
                          dict(pool=pool, personal=personal, scrapes=scrapes))


async def _account_chosen(message: Message, state: FSMContext, account, flow: str, deps: dict):
    await state.update_data(account=account.path)
    if account.personal:
        await message.answer(PERSONAL_WARNING + (BASE_NOTE if flow in _BASE_FLOWS else ""))
    await FLOW_NEXT[flow](message, state, deps)


def run_account(pool: WorkerPool, personal, path: str):
    return find_account(_accounts(pool, personal), path)


async def lookup(message: Message, pool: WorkerPool, personal, path: str, chats: list[str]):
    """The chats' names on the flow's account (see lookup_on), asked on its own client (see borrow);
    None when they can't be asked now (said why, unless the account is gone) — the flow then goes on
    with the input as typed."""
    account = run_account(pool, personal, path)
    if account is None:
        return None
    # one account, one job (see WorkerPool): not even a lookup on one that is busy
    if pool.busy(path) or (pool.scraping is not None and pool.scraping.path == path):
        await message.answer("ℹ️ Названия не проверены: аккаунт сейчас занят.")
        return None
    status = await message.answer("🔎 Проверяю названия…")
    try:
        infos = await borrow(pool, account.client, path, lambda: lookup_on(account.client, chats),
                             CHAT_LOOKUP_TIMEOUT)
    except (Exception, SystemExit, asyncio.CancelledError) as err:  # SystemExit: a logged-out session
        if isinstance(err, asyncio.CancelledError) and asyncio.current_task().cancelling():
            raise  # the bot is stopping
        # a CancelledError else: a job's end released the account mid-check (Telethon cancels a
        # disconnected client's requests)
        reason = "соединение прервано" if isinstance(err, asyncio.CancelledError) else str(err)
        try:  # the 🔎 becomes the reason
            await status.edit_text(f"ℹ️ Названия не проверены: {reason or 'Telegram не ответил вовремя'}")
        except Exception:
            pass
        return None
    try:  # the names come next, in its place
        await status.delete()
    except Exception:
        pass
    return infos


async def check_chats(message: Message, state: FSMContext, pool: WorkerPool, personal,
                       chats: list[str], project: str | None = None) -> bool:
    """Show the typed chats' names; True to go on to the flow's next step. With one not found
    the operator decides: ▶️ goes on anyway (chats_ok), a new list is checked again. `project`:
    the names found go to its card (modules/projects.remember)."""
    infos = await lookup(message, pool, personal, (await state.get_data()).get("account", ""), chats)
    await state.update_data(chat_labels=infos and [chat_label(info) for info in infos])
    if infos is None:
        return True
    if project:
        projects.remember(project, infos)
    # a block per chat (chat_lines): a long list goes in several messages, each within Telegram's 4096
    await send_chunked(message, "", [chat_block(i, info, html=True) for i, info in enumerate(infos, 1)])
    if all(info.problem is None for info in infos):
        return True
    await message.answer("Пришлите список заново — или продолжите как есть:",
                         reply_markup=cancel_kb(await state.get_state(),
                                                choice_kb("chats_ok", [("▶️ Продолжить всё равно", "go")])))
    return False


@router.callback_query(ChoiceCB.filter(F.scope == "chats_ok"))
async def chats_ok(callback: CallbackQuery, state: FSMContext, pool: WorkerPool, personal, scrapes: JobManager):
    await callback.answer()
    step = CHATS_NEXT.get(await state.get_state())
    if step is None:  # a stale button
        await callback.message.answer(STALE_BUTTON)
        return
    await step(callback.message, state, dict(pool=pool, personal=personal, scrapes=scrapes))


async def take_slot(scrapes: JobManager, pool: WorkerPool, account, label: str, menu, answer) -> Progress | None:
    """The scraper's slot for a job on `account` (a worker is kept out of bot jobs meanwhile):
    the job's 📊 Progress, or None after telling why (menu: with the main menu). Its ⏹ is the
    slot's soft stop: the job takes it with scrapes.soft_stop()."""
    if pool.busy(account.path):  # a personal account: only while borrow() uses it (codes, a names check)
        await menu(PERSONAL_BUSY if account.personal else WORKER_BUSY)
        return None
    if not scrapes.acquire(label, cancelable=False, timeout=0):
        await answer(f"⛔ Скрапер занят: {scrapes.label}. Дождитесь завершения.")
        return None
    pool.scraping = account  # new bot jobs run without it (a worker); its login codes wait
    progress = scrapes.progress = Progress()
    progress.prepare()  # connecting, resolving, loading the file come first
    return progress


async def free_slot(scrapes: JobManager, pool: WorkerPool, status):
    """The job is over: its ⏹, its slot and its worker are free."""
    pool.scraping = None
    await drop_button(status)
    scrapes.release()  # last: stop_for_shutdown waits for it, and this was the job's last bot call


async def date_or_ask(message: Message, raw: str | None, end_of_day: bool = False) -> bool:
    """True if `raw` is a date; else says why and the step stays (a later step couldn't fix it)."""
    if raw is None:
        return False
    try:
        parse_date(raw, end_of_day=end_of_day)
    except SystemExit as err:
        await message.answer(f"{err} Пришлите дату ещё раз:")
        return False
    return True


@router.callback_query(ChoiceCB.filter(F.scope == "scrape_stop"))
async def scrape_stop(callback: CallbackQuery, scrapes: JobManager):
    """⏹ under a scrape, a members list or a verify: the slot's job stops (see take_slot)."""
    if await scrapes.stop():
        await callback.answer("Останавливаю… собранное сохранится.")
    else:
        await callback.answer("Нечего останавливать.")
