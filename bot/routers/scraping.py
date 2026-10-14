import asyncio
import html
import os
import secrets
import threading
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message

from bot.callbacks import ChoiceCB
from bot.keyboards.common import cancel_kb, choice_kb, paged_kb, scrape_kb
from bot.keyboards.menu import main_menu
from bot.routers._common import STALE_BUTTON, menu_answer, require_text, send_chunked
from bot.services.delegation import WorkerPool, borrow
from bot.services.jobs import JobManager
from bot.services import scraping
from bot.services.progress import Progress
from bot.services.registry import menu_path
from modules import json_file, projects, scraped_files
from bot.states import Analysis, Members, Scrape, Verify
from modules.scraper_creds import build_credentials, find_account, scrape_accounts

from scraper import analysis
from scraper.datafiles import read_table
from scraper.members import MembersParams
from scraper.scrape import (
    CHAT_LOOKUP_TIMEOUT, ScrapeParams, bad_channels, bad_channels_text, chat_block, chat_label,
    lookup_on, parse_channels, parse_date, pending_resume, resume_params,
)
from scraper.verify import VerifyParams

router = Router()

DEFAULT_OUT = scraped_files.BASES_DIR


def read_preview(df, rows: int | None = None) -> str:
    """HTML-safe <pre> preview of a dataframe head for Telegram (escaped + length-capped);
    rows: the file's row count, when df is only its head."""
    text = df.head(10).to_string()
    # cut before escaping (can't split an &amp;), in UTF-16 units like Telegram's limit
    raw = text.encode("utf-16-le")
    if len(raw) > 3500 * 2:
        text = raw[:3500 * 2].decode("utf-16-le", errors="ignore") + "…"
    return f"<pre>{html.escape(text)}</pre>\n[{len(df) if rows is None else rows} строк x {len(df.columns)} столбцов]"


async def _drop_button(status):
    """The job is over: left in place, its 📊 button would show whatever job runs next."""
    if status is None:
        return
    try:
        await status.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


async def _send_files(bot, chat_id, paths):
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


# =============================== accounts ===============================
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


async def _choose_account(message: Message, state: FSMContext, flow: str, **deps):
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
    await _FLOW_NEXT[flow](message, state, deps)


def _run_account(pool: WorkerPool, personal, path: str):
    return find_account(_accounts(pool, personal), path)


async def _lookup(message: Message, pool: WorkerPool, personal, path: str, chats: list[str]):
    """The chats' names on the flow's account (see lookup_on), asked on its own client (see borrow);
    None when they can't be asked now (said why, unless the account is gone) — the flow then goes on
    with the input as typed."""
    account = _run_account(pool, personal, path)
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


async def _check_chats(message: Message, state: FSMContext, pool: WorkerPool, personal,
                       chats: list[str], project: str | None = None) -> bool:
    """Show the typed chats' names; True to go on to the flow's next step. With one not found
    the operator decides: ▶️ goes on anyway (chats_ok), a new list is checked again. `project`:
    the names found go to its card (modules/projects.remember)."""
    infos = await _lookup(message, pool, personal, (await state.get_data()).get("account", ""), chats)
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
    step = _CHATS_NEXT.get(await state.get_state())
    if step is None:  # a stale button
        await callback.message.answer(STALE_BUTTON)
        return
    await step(callback.message, state, dict(pool=pool, personal=personal, scrapes=scrapes))


# =============================== scrape ===============================
# A bot scrape in progress (scraped_files.SCRAPE_MARKER), continued automatically after a bot
# restart (see resume_after_restart): kept on an interruption or a shutdown, dropped once the
# scrape ends for good (finished, stopped by the operator, bad input).
# automatic resumes in a row that made no progress before auto-resume gives up: a run killed
# at the same spot each time (e.g. out of memory in its final step) must not crash-loop the bot
MAX_STALLED_RESUMES = 2

# the ⏹ switch of the scraper slot's job (scrape / members / verify: one at a time)
_job_stop: threading.Event | None = None
_user_stopped = False   # the operator pressed ⏹ (vs. the bot shutting down)
_shutting_down = False
_resume_task: asyncio.Task | None = None  # resume_after_restart's run


def _arm_stop(params) -> threading.Event:
    """A fresh ⏹ switch for the job about to run with `params` (its .stop)."""
    global _job_stop, _user_stopped
    _job_stop = params.stop = threading.Event()
    _user_stopped = False
    return _job_stop


def _disarm_stop(stop: threading.Event):
    global _job_stop
    if _job_stop is stop:
        _job_stop = None


async def _take_slot(scrapes: JobManager, pool: WorkerPool, account, label: str, menu, answer) -> Progress | None:
    """The scraper's slot for a job on `account` (a worker is kept out of bot jobs meanwhile):
    the job's 📊 Progress, or None after telling why (menu: with the main menu)."""
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


async def _free_slot(scrapes: JobManager, pool: WorkerPool, stop: threading.Event, status):
    """The job is over: its ⏹, its slot and its worker are free."""
    scrapes.release()
    pool.scraping = None
    await _drop_button(status)
    _disarm_stop(stop)  # last: stop_for_shutdown waits for it


def _write_marker(marker: dict):
    json_file.save(scraped_files.SCRAPE_MARKER, marker)


def _read_marker() -> dict | None:
    try:
        return json_file.load(scraped_files.SCRAPE_MARKER, None)
    except (OSError, ValueError):  # a half-written / broken marker: nothing to continue
        return None


def _drop_marker():
    try:
        os.remove(scraped_files.SCRAPE_MARKER)
    except OSError:
        pass


async def scrape_project(message: Message, state: FSMContext, project, deps: dict):
    """A project's scrape (bot/routers/projects.py): its name and channels are the project's,
    the channels are named on the chosen account as if typed, the rest is asked as usual."""
    await state.clear()
    await state.update_data(name=project.name, out_dir=DEFAULT_OUT, channels=", ".join(project.channels))
    await _scrape_named(message, state, deps, "project")


async def topup_project(message: Message, state: FSMContext, project, plan, deps: dict):
    """A project's top-up (bot/routers/projects.py): the previous scrape's channels, keyword and
    dates come from its posts file (modules/projects.topup); the account is asked, then the summary."""
    if pending_resume(DEFAULT_OUT, project.name) is not None:  # a fresh run would drop its checkpoint
        await menu_answer(message, "У проекта незавершённый скрап — сначала продолжите его: «▶️ Скрап проекта».")
        return
    await state.clear()
    await state.update_data(name=project.name, out_dir=DEFAULT_OUT, channels=", ".join(plan.channels),
                            date_min=f"{plan.date_min:%d.%m.%Y}", date_max=f"{plan.date_max:%d.%m.%Y}",
                            keyword=plan.keyword, extend=plan.posts)
    await _choose_account(message, state, "topup", **deps)


async def _scrape_named(message: Message, state: FSMContext, deps: dict, flow: str):
    """The scrape's name is set: offer to continue an interrupted scrape under it, else ask the account."""
    name = (await state.get_data())["name"]
    meta = pending_resume(DEFAULT_OUT, name)
    if meta is not None:  # an interrupted scrape under this name: offer to continue it
        await state.set_state(Scrape.resume)
        await message.answer(
            f"Под этим именем есть незавершённый скрап «{html.escape(meta['name'])}».\n\n"
            f"Каналы:\n\n{projects.chat_list(projects.find(name), meta['channels'], html=True)}\n\n"
            f"Период: {meta['date_min'][:10]} – {meta['date_max'][:10]}\n"
            f"Уже собрано постов: {meta.get('t_index', 0)}",
            parse_mode="HTML",
            reply_markup=choice_kb("scrape_resume", [
                ("▶️ Продолжить", "continue"),
                ("🆕 Начать заново (собранное удалится)", "fresh"),
            ]),
        )
        return
    await _choose_account(message, state, flow, **deps)


@router.callback_query(Scrape.resume, ChoiceCB.filter(F.scope == "scrape_resume"))
async def scrape_resume(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext,
                        pool: WorkerPool, personal, scrapes: JobManager):
    await callback.answer()
    deps = dict(pool=pool, personal=personal, scrapes=scrapes)
    data = await state.get_data()
    if callback_data.value == "fresh":  # a project's scrape keeps its channels
        await _choose_account(callback.message, state, "project", **deps)
        return
    await state.clear()
    meta = pending_resume(data["out_dir"], data["name"])
    if meta is None:  # finished or removed meanwhile
        await menu_answer(callback.message, "Незавершённый скрап уже не найден.")
        return
    params = resume_params(meta, data["out_dir"])
    if not params.account:  # a checkpoint from before accounts were stored: ask, then continue
        await state.update_data(**data)
        await _choose_account(callback.message, state, "scrape_resume", **deps)
        return
    await _run_scrape(callback.bot, callback.message.chat.id, scrapes, pool, personal, params)


async def _resume_on_chosen(message: Message, state: FSMContext, deps: dict):
    """scrape_resume, for a checkpoint with no account stored: on the account just chosen."""
    data = await state.get_data()
    await state.clear()
    meta = pending_resume(data["out_dir"], data["name"])
    if meta is None:
        await menu_answer(message, "Незавершённый скрап уже не найден.")
        return
    params = resume_params(meta, data["out_dir"])
    params.account = data["account"]
    await _run_scrape(message.bot, message.chat.id, deps["scrapes"], deps["pool"], deps["personal"], params)


@router.message(Scrape.channels)
async def scrape_channels(message: Message, state: FSMContext, pool: WorkerPool, personal):
    channels = await require_text(message)
    if channels is None:
        return
    if not parse_channels(channels):
        await message.answer("Не вижу ни одного канала. Пришлите ещё раз:")
        return
    if bad := bad_channels(parse_channels(channels)):
        await message.answer(f"{bad_channels_text(bad)} Пришлите список ещё раз:")
        return
    await state.update_data(channels=channels)
    # a list corrected for this run: those of the project's own channels in it get named on its card
    if await _check_chats(message, state, pool, personal, parse_channels(channels),
                          (await state.get_data()).get("name")):
        await _ask_date_min(message, state)


async def _check_project_channels(message: Message, state: FSMContext, deps: dict):
    """A project's scrape: its channels are checked on the chosen account as if just typed (a
    corrected list sent now is for this run only)."""
    await state.set_state(Scrape.channels)
    data = await state.get_data()
    if await _check_chats(message, state, deps["pool"], deps["personal"], parse_channels(data["channels"]),
                          data.get("name")):
        await _ask_date_min(message, state)


async def _ask_date_min(message: Message, state: FSMContext, deps: dict | None = None):
    await state.set_state(Scrape.date_min)
    await message.answer("Дата с (ДД.ММ.ГГГГ или ГГГГ-ММ-ДД):", reply_markup=cancel_kb(Scrape.date_min))


@router.message(Scrape.date_min)
async def scrape_dmin(message: Message, state: FSMContext):
    date_min = await require_text(message)
    if date_min is None:
        return
    await state.update_data(date_min=date_min)
    await state.set_state(Scrape.date_max)
    await message.answer("Дата по (ДД.ММ.ГГГГ или ГГГГ-ММ-ДД):", reply_markup=cancel_kb(Scrape.date_max))


def _scrape_params(data: dict) -> ScrapeParams:
    """SystemExit / ValueError on bad input (parse_date raises SystemExit on a bad date)."""
    return ScrapeParams(
        channels=parse_channels(data["channels"]),
        date_min=parse_date(data["date_min"]),
        date_max=parse_date(data["date_max"], end_of_day=True),
        name=data["name"],
        keyword=data.get("keyword", ""),
        max_messages=data.get("max_messages") or ScrapeParams.max_messages,
        out_dir=Path(data["out_dir"]),
        with_comments=data.get("comments", True),
        with_reactors=data.get("reactors", True),
        with_participants=data.get("participants", True),
        account=data["account"],
        chat_labels=data.get("chat_labels"),
        extend=data.get("extend", ""),
    )


@router.message(Scrape.date_max)
async def scrape_run(message: Message, state: FSMContext):
    date_max = await require_text(message)
    if date_max is None:
        return

    data = {**await state.get_data(), "date_max": date_max}
    try:
        params = _scrape_params(data)
    except (Exception, SystemExit) as err:
        await message.answer(f"Неверные параметры: {err}")  # the date can be sent again
        return

    if params.date_min > params.date_max:
        await message.answer("Дата 'с' позже даты 'по'. Пришлите дату 'по' ещё раз.")
        return

    await state.update_data(date_max=date_max)
    await _ask_go(message, state)


async def _ask_go(message: Message, state: FSMContext, deps: dict | None = None):
    """The summary before a scrape starts: ▶️ Запустить or ⚙️ Дополнительно (a top-up's, too)."""
    data = await state.get_data()
    await state.set_state(Scrape.confirm)
    folder = html.escape(f"{data['out_dir']}/{data['name']}")
    if data.get("extend"):  # its keyword is the previous scrape's: no filter to set
        files = (f"Дособор к {html.escape(os.path.basename(data['extend']))}: новое вольётся в файлы проекта, "
                 f"заменённые уйдут в {folder}_previous/\n\n"
                 "«Дополнительно» — комментарии, реакции, база участников, лимит постов.")
    else:
        files = (f"Файлы: {folder}_…\n\n"
                 "«Дополнительно» — комментарии, реакции, база участников, фильтр по слову, лимит постов.")
    await message.answer(
        f"Каналы:\n\n{projects.chat_list(projects.find(data['name']), parse_channels(data['channels']), html=True)}"
        "\n\n"
        f"Период: {html.escape(data['date_min'])} – {html.escape(data['date_max'])}\n" + files,
        parse_mode="HTML",
        reply_markup=choice_kb("scrape_go", [("▶️ Запустить", "run"), ("⚙️ Дополнительно", "more")]),
    )


def _settings_kb(data: dict):
    def mark(key):
        return "✅" if data.get(key, True) else "❌"
    keyword = data.get("keyword") or "—"
    limit = data.get("max_messages") or "все"
    return choice_kb("scrape_opt", [
        (f"💬 Комментарии {mark('comments')}", "comments"),
        (f"❤️ Реакции {mark('reactors')} (медленно)", "reactors"),
        (f"👥 База участников {mark('participants')}", "participants"),
        # a top-up keeps the previous scrape's keyword (scraper.scrape.run refuses another)
        *([] if data.get("extend") else [(f"🔎 Фильтр по слову: {keyword[:24]}", "keyword")]),
        (f"🔢 Максимум постов: {limit}", "max"),
        ("▶️ Запустить", "run"),
    ])


async def _show_settings(message: Message, state: FSMContext):
    await state.set_state(Scrape.confirm)
    await message.answer("Настройки скрапа (нажмите, чтобы изменить):",
                         reply_markup=_settings_kb(await state.get_data()))


@router.callback_query(Scrape.confirm, ChoiceCB.filter(F.scope == "scrape_go"))
async def scrape_go(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext,
                    pool: WorkerPool, personal, scrapes: JobManager):
    await callback.answer()
    if callback_data.value == "more":
        await _show_settings(callback.message, state)
        return
    await _start_scrape(callback, state, pool, personal, scrapes)


@router.callback_query(Scrape.confirm, ChoiceCB.filter(F.scope == "scrape_opt"))
async def scrape_option(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext,
                        pool: WorkerPool, personal, scrapes: JobManager):
    await callback.answer()
    option = callback_data.value
    if option in ("comments", "reactors", "participants"):
        data = await state.get_data()
        await state.update_data(**{option: not data.get(option, True)})
        try:  # in place: toggling must not pile up messages
            await callback.message.edit_reply_markup(reply_markup=_settings_kb(await state.get_data()))
        except Exception:
            pass
    elif option == "keyword":
        await state.set_state(Scrape.keyword)
        await callback.message.answer("Фильтр по слову: только посты с ним («-» = без фильтра):",
                                      reply_markup=cancel_kb(Scrape.keyword))
    elif option == "max":
        await state.set_state(Scrape.max_messages)
        await callback.message.answer("Максимум постов (число, «-» = все):",
                                      reply_markup=cancel_kb(Scrape.max_messages))
    elif option == "run":
        await _start_scrape(callback, state, pool, personal, scrapes)


@router.message(Scrape.keyword)
async def scrape_keyword(message: Message, state: FSMContext):
    keyword = await require_text(message)
    if keyword is None:
        return
    await state.update_data(keyword="" if keyword.strip() == "-" else keyword.strip())
    await _show_settings(message, state)


@router.message(Scrape.max_messages)
async def scrape_max(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if raw == "-":
        await state.update_data(max_messages=None)
    elif raw.isdecimal() and int(raw) > 0:
        await state.update_data(max_messages=int(raw))
    else:
        await message.answer("Нужно число больше 0 или «-»:")
        return
    await _show_settings(message, state)


async def _start_scrape(callback: CallbackQuery, state: FSMContext, pool: WorkerPool, personal,
                        scrapes: JobManager):
    data = await state.get_data()
    await state.clear()
    try:
        params = _scrape_params(data)
    except (Exception, SystemExit) as err:
        await menu_answer(callback.message, f"Неверные параметры: {err}")
        return
    await _run_scrape(callback.bot, callback.message.chat.id, scrapes, pool, personal, params)


@router.callback_query(ChoiceCB.filter(F.scope == "scrape_stop"))
async def scrape_stop(callback: CallbackQuery):
    global _user_stopped
    if _job_stop is None or _job_stop.is_set():
        await callback.answer("Нечего останавливать.")
        return
    _user_stopped = True
    _job_stop.set()
    await callback.answer("Останавливаю… собранное сохранится.")


async def _run_scrape(bot, chat_id: int, scrapes: JobManager, pool: WorkerPool, personal,
                      params: ScrapeParams, resumes: dict | None = None):
    """resumes: the automatic-resume record kept in the marker (see resume_after_restart)."""
    async def menu(text):
        await bot.send_message(chat_id, text, reply_markup=main_menu())

    account = _run_account(pool, personal, params.account)
    if account is None:
        await menu(f"Аккаунт {params.account} не найден: скрап с него можно продолжить только на нём.")
        return

    out_dir = str(params.out_dir)
    # before acquire: a failing snapshot must not hold the non-cancelable slot
    try:
        before = scraping.dir_snapshot(out_dir)
    except OSError as err:
        await menu(f"Папка вывода недоступна: {err}")
        return

    progress = await _take_slot(scrapes, pool, account, "Скрап", menu,
                                lambda text: bot.send_message(chat_id, text))
    if progress is None:
        return
    params.on_progress = lambda frac, eta, posts: progress.set(frac, eta, f"Постов: {posts}")
    stop = _arm_stop(params)
    status = None
    try:  # from here on, any error frees the slot and the worker (see finally)
        _write_marker({"out_dir": out_dir, "name": params.name, "account": params.account, "chat_id": chat_id,
                       **(resumes or {})})
        status = await bot.send_message(
            chat_id, f"Скрап запущен на аккаунте {account.label} — может занять долго.\n"
                     f"Остальные функции бота доступны{'' if account.personal else ' — без этого воркера'}.",
            reply_markup=scrape_kb())
        await scraping.do_scrape(build_credentials(account.client), params)
    except SystemExit as err:
        # a str code is a user-input error with its reason; an int one an interrupted run,
        # which exits before any output file is written
        if isinstance(err.code, str) and err.code:
            _drop_marker()
            await menu(f"Скрап остановлен: {err}")
        elif _user_stopped:
            _drop_marker()
            await menu("⏹ Скрап остановлен. Собранное сохранено: запустите скрап с тем же именем "
                       "— бот предложит продолжить.")
        elif not _shutting_down:  # interrupted: the marker stays for a restart to continue it
            await menu("Скрап прерван (сеть / флуд-бан). Собранное сохранено: запустите скрап "
                       "с тем же именем — бот предложит продолжить.")
        return
    except Exception as err:  # e.g. the scrape's process killed (out of memory)
        _drop_marker()
        saved = (" Собранное сохранено: запустите скрап с тем же именем — бот предложит "
                 "продолжить.") if pending_resume(out_dir, params.name) else ""
        await menu(f"Ошибка скрапа: {err}.{saved}")
        return
    finally:
        await _free_slot(scrapes, pool, stop, status)

    _drop_marker()
    files = scraping.new_files(out_dir, before)
    if files:
        await bot.send_message(chat_id, f"Готово ✅ Файлов: {len(files)}")
        await _send_files(bot, chat_id, files)
        await menu("Скрап завершён. Вы в главном меню.")
    else:
        await menu("Готово, но новых файлов не найдено.")


async def resume_after_restart(bot, scrapes: JobManager, pool: WorkerPool, personal):
    """Bot startup: continue the scrape a restart interrupted (see scraped_files.SCRAPE_MARKER)."""
    marker = _read_marker()
    if marker is None:
        return
    meta = pending_resume(marker["out_dir"], marker["name"])
    if meta is None:  # finished or removed meanwhile
        _drop_marker()
        return
    # progress since the last automatic resume, by the checkpoint's post count; a clean
    # shutdown (systemd / needrestart restarts) is no crash and doesn't count as a stall
    t_index = meta.get("t_index", 0)
    clean = marker.pop("clean_shutdown", False)
    stalls = marker.get("stalls", 0) + (not clean) if marker.get("t_index") == t_index else 0
    if stalls >= MAX_STALLED_RESUMES:
        _drop_marker()
        try:
            await bot.send_message(
                marker["chat_id"],
                f"⚠️ Автопродолжение скрапа «{marker['name']}» отключено: он падал при каждом запуске "
                "на одном месте (вероятно, не хватает памяти на финальную сборку). Собранное сохранено — "
                "продолжите вручную: скрап с тем же именем.")
        except Exception:
            pass
        return

    params = resume_params(meta, marker["out_dir"])
    params.account = params.account or marker.get("account", "")
    if _run_account(pool, personal, params.account) is None:
        _drop_marker()  # its account is gone for good: _run_scrape says so once, no retries
    else:  # counted now: a run that returns before writing its own marker must still count
        _write_marker({**marker, "t_index": t_index, "stalls": stalls})

    async def run():
        try:
            await bot.send_message(marker["chat_id"], f"🔄 Бот перезапущен — продолжаю скрап «{params.name}».")
        except Exception:
            pass
        await _run_scrape(bot, marker["chat_id"], scrapes, pool, personal, params,
                          resumes={"t_index": t_index, "stalls": stalls})

    global _resume_task
    # don't hold up the bot's start; kept referenced, or the loop's weak ref lets GC drop it
    _resume_task = asyncio.create_task(run())


async def stop_for_shutdown():
    """Bot shutdown: stop the scraper slot's job; a scrape checkpoints and its marker stays,
    so the next start continues it."""
    global _shutting_down
    _shutting_down = True
    marker = _read_marker()
    if marker is not None:  # see resume_after_restart: a crash never gets here
        _write_marker({**marker, "clean_shutdown": True})
    if _job_stop is not None:
        _job_stop.set()
        # its last bot call (_free_slot) must come before aiogram closes the bot's session, or
        # it opens a new one nobody closes; within systemd's TimeoutStopSec (30 s)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + scraping.CHILD_STOP_GRACE + 5
        while _job_stop is not None and loop.time() < deadline:
            await asyncio.sleep(0.1)


# =============================== members ===============================

async def members_project(message: Message, state: FSMContext, project, deps: dict):
    """The members from a project's card (bot/routers/projects.py): the base is named after the
    project, so it is one of the project's bases (scraped_files.project_files)."""
    await state.clear()
    await state.update_data(only_project=project.name)
    await _choose_account(message, state, "members", **deps)


async def _ask_groups(message: Message, state: FSMContext, deps: dict):
    await state.set_state(Members.chats)
    project = (await state.get_data())["only_project"]
    note = f"\n\nБаза ляжет в проект «{html.escape(project)}»"
    note += (" — новые участники добавятся к его базе, повторы не дублируются."
             if scraped_files.members_base(project).exists() else ".")
    await message.answer(
        "Группы через запятую (@name / t.me ссылка / инвайт t.me/+… / числовой id).\n"
        "Аккаунт должен состоять в группе. Подписчики канала доступны только админам." + note,
        parse_mode="HTML", reply_markup=cancel_kb(Members.chats),
    )


@router.message(Members.chats)
async def members_chats(message: Message, state: FSMContext, pool: WorkerPool, personal, scrapes: JobManager):
    chats = await require_text(message)
    if chats is None:
        return
    if not parse_channels(chats):
        await message.answer("Не вижу ни одной группы. Пришлите ещё раз:")
        return
    if bad := bad_channels(parse_channels(chats)):
        await message.answer(f"{bad_channels_text(bad)} Пришлите список групп ещё раз:")
        return
    await state.update_data(chats=chats)
    # the groups get named on the project too: its messages show them by name
    if await _check_chats(message, state, pool, personal, parse_channels(chats),
                          (await state.get_data()).get("only_project")):
        await _members_ready(message, state, dict(pool=pool, personal=personal, scrapes=scrapes))


async def _members_ready(message: Message, state: FSMContext, deps: dict):
    """The groups are checked: the project's base, named after it, is collected."""
    await _members_go(message, state, (await state.get_data())["only_project"], **deps)


async def _members_go(message: Message, state: FSMContext, name: str, pool: WorkerPool, personal,
                      scrapes: JobManager):
    data = await state.get_data()
    await state.clear()

    account = _run_account(pool, personal, data.get("account", ""))
    if account is None:
        await menu_answer(message, "Аккаунт не найден, начните заново.")
        return

    progress = await _take_slot(scrapes, pool, account, "Участники чата",
                                lambda text: menu_answer(message, text), message.answer)
    if progress is None:
        return
    before = scraped_files.row_count(scraped_files.members_base(name))  # the base collected before: added to
    params = MembersParams(chats=parse_channels(data["chats"]), name=name,
                           out_dir=Path(DEFAULT_OUT), on_progress=progress.update)
    stop = _arm_stop(params)
    status = None
    try:
        status = await message.answer("Собираю участников…", reply_markup=scrape_kb())
        path, problems = await scraping.do_members(build_credentials(account.client), params)
    except (Exception, SystemExit) as err:  # connect() raises SystemExit on a logged-out session
        await menu_answer(message, f"Ошибка: {err}")
        return
    finally:
        await _free_slot(scrapes, pool, stop, status)

    if problems:
        why = {chat: str(problem) for chat, problem in problems}
        named = projects.chat_list(projects.find(data["only_project"]), list(why), html=True, problems=why)
        await message.answer(f"Не всё удалось собрать:\n\n{named}", parse_mode="HTML")
    if path is None:
        await menu_answer(message, "Ни одного участника не собрано.")
        return
    await _send_files(message.bot, message.chat.id, [str(path)])
    total = scraped_files.row_count(path)
    where = (f"в проекте «{data['only_project']}» (новых: {total - before}, всего: {total}): 📇 В контакты и "
             "📨 ЛС по базе — в его карточке")
    await menu_answer(message, f"⏹ Остановлено. Собранные участники сохранены — база {where}." if _user_stopped else
                f"Готово ✅ База — {where}.")


# =============================== verify ===============================

async def verify_project(message: Message, state: FSMContext, project, deps: dict):
    """The verify from a project's card (bot/routers/projects.py): its posts files only."""
    await state.clear()
    await state.update_data(only_project=project.name)
    await _choose_account(message, state, "verify", **deps)


async def _ask_posts_file(message: Message, state: FSMContext, deps: dict):
    await state.set_state(Verify.input)
    files = scraped_files.project_files((await state.get_data())["only_project"], kinds=("posts",))
    await state.update_data(files=[path for path, _ in files])  # buttons carry an index into this
    if files:
        await message.answer(
            "Выберите файл с постами или пришлите путь к файлу/папке:",
            reply_markup=cancel_kb(Verify.input, choice_kb(
                "verify_base", [(f"📄 {label}", str(i)) for i, (_, label) in enumerate(files)])),
        )
    else:
        await message.answer(
            "У проекта нет файлов с постами. Пришлите путь к файлу или папке:",
            reply_markup=cancel_kb(Verify.input),
        )


def _ddmmyyyy(iso_date: str) -> str:
    year, month, day = iso_date.split("-")
    return f"{day}.{month}.{year}"


async def _ask_channel(message: Message, state: FSMContext, pool: WorkerPool, personal, input_: str):
    """Offer the posts file's channels as buttons (named by their titles); with the scrape's
    window known, a button runs the verify at once."""
    channels, window = await asyncio.to_thread(scraped_files.verify_presets, input_)
    await state.update_data(input=input_, channels=channels, window=window)
    await state.set_state(Verify.channel)

    if not channels:
        await message.answer("Канал (@name / t.me / числовой id):", reply_markup=cancel_kb(Verify.channel))
        return

    data = await state.get_data()
    infos = await _lookup(message, pool, personal, data.get("account", ""), channels)
    if infos:  # the project's channels get named on its card
        projects.remember(data["only_project"], infos)
    names = [info.title or info.raw for info in infos] if infos else channels
    # a channel that can't be read now: still a button (the operator decides), marked, with why
    bad = [info.problem is not None for info in infos] if infos else [False] * len(channels)
    if window:
        span = f"{_ddmmyyyy(window[0])}–{_ddmmyyyy(window[1])}"
        text = (f"Каналы из файла — кнопка сразу запустит проверку за период скрапа ({span}).\n"
                "Другой канал или другие даты — введите канал текстом:")
        labels = [f"{'⚠️' if b else '▶️'} {name} · {span}" for name, b in zip(names, bad)]
    else:
        text = "Канал из файла — или введите другой (@name / t.me / числовой id):"
        labels = [f"{'⚠️' if b else '📢'} {name}" for name, b in zip(names, bad)]
    if any(bad):
        # numbered as the buttons above
        text += "\n\n" + "\n\n".join(chat_block(i, info, html=True) for i, info in enumerate(infos, 1)
                                       if info.problem)
    await message.answer(
        text, parse_mode="HTML", reply_markup=cancel_kb(Verify.channel,
                                     choice_kb("verify_channel", [(label, str(i)) for i, label in enumerate(labels)]))
    )


@router.callback_query(Verify.input, ChoiceCB.filter(F.scope == "verify_base"))
async def verify_base(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext,
                      pool: WorkerPool, personal):
    await callback.answer()
    files = (await state.get_data()).get("files", [])
    index = int(callback_data.value)
    if not 0 <= index < len(files):
        return
    await _ask_channel(callback.message, state, pool, personal, files[index])


@router.message(Verify.input)
async def verify_input(message: Message, state: FSMContext, pool: WorkerPool, personal):
    input_ = await require_text(message)
    if input_ is None:
        return
    await _ask_channel(message, state, pool, personal, input_)


@router.callback_query(Verify.channel, ChoiceCB.filter(F.scope == "verify_channel"))
async def verify_channel_pick(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext,
                              pool: WorkerPool, personal, scrapes: JobManager):
    await callback.answer()
    data = await state.get_data()
    channels = data.get("channels", [])
    index = int(callback_data.value)
    if not 0 <= index < len(channels):
        return

    window = data.get("window")
    if window:
        await state.clear()
        await _run_verify(callback.message, scrapes, pool, _run_account(pool, personal, data.get("account", "")),
                          data["input"], channels[index], *window)
        return

    await state.update_data(channel=channels[index])
    await state.set_state(Verify.date_min)
    await callback.message.answer("Дата с:", reply_markup=cancel_kb(Verify.date_min))


@router.message(Verify.channel)
async def verify_channel(message: Message, state: FSMContext, pool: WorkerPool, personal):
    channel = await require_text(message)
    if channel is None:
        return
    if bad_channels([channel]):
        await message.answer(f"{bad_channels_text([channel])} Пришлите канал ещё раз:")
        return
    await state.update_data(channel=channel)
    if await _check_chats(message, state, pool, personal, [channel], (await state.get_data()).get("only_project")):
        await _ask_verify_dmin(message, state)


async def _ask_verify_dmin(message: Message, state: FSMContext, deps: dict | None = None):
    await state.set_state(Verify.date_min)
    await message.answer("Дата с:", reply_markup=cancel_kb(Verify.date_min))


@router.message(Verify.date_min)
async def verify_dmin(message: Message, state: FSMContext):
    date_min = await require_text(message)
    if date_min is None:
        return
    await state.update_data(date_min=date_min)
    await state.set_state(Verify.date_max)
    await message.answer("Дата по:", reply_markup=cancel_kb(Verify.date_max))


@router.message(Verify.date_max)
async def verify_run(message: Message, state: FSMContext, pool: WorkerPool, personal,
                     scrapes: JobManager):
    date_max = await require_text(message)
    if date_max is None:
        return

    data = await state.get_data()
    await state.clear()
    await _run_verify(message, scrapes, pool, _run_account(pool, personal, data.get("account", "")),
                      data["input"], data["channel"], data["date_min"], date_max)


async def _run_verify(message: Message, scrapes: JobManager, pool: WorkerPool, account,
                      input_: str, channel: str, date_min: str, date_max: str):
    if account is None:
        await menu_answer(message, "Аккаунт не найден, начните заново.")
        return

    try:
        output = scraped_files.missed_path(input_, channel)  # ValueError on "."
        params = VerifyParams(
            input=input_,
            channel=channel,
            date_min=parse_date(date_min),
            date_max=parse_date(date_max, end_of_day=True),
            output=output,
            comment_sample=0,
        )
    except (Exception, SystemExit) as err:  # parse_date raises SystemExit on a bad date
        await message.answer(f"Неверные параметры: {err}")
        return

    if params.date_min > params.date_max:
        await message.answer("Дата 'с' позже даты 'по'.")
        return

    progress = await _take_slot(scrapes, pool, account, "Верификация",
                                lambda text: menu_answer(message, text), message.answer)
    if progress is None:
        return
    params.on_progress = progress.update
    stop = _arm_stop(params)
    status = None
    interrupted = False
    bad_input = None
    try:
        status = await message.answer("Верификация запущена…", reply_markup=scrape_kb())

        # Drop a stale <input>_missed from an earlier run first, so the file existing after
        # the run cleanly means THIS run wrote it (mtime can't tell two runs in one second apart).
        if os.path.exists(output):
            os.remove(output)

        await scraping.do_verify(build_credentials(account.client), params)
    except SystemExit as err:
        # verify.run raises SystemExit(1) (int code) both when it FOUND missed posts
        # (after writing the results file) and when the run was interrupted (no file).
        # A str code is a user-input error (unknown channel / no posts file / no rows
        # for the group) carrying the real message — surface it instead of "прервана".
        if isinstance(err.code, str) and err.code:
            bad_input = err.code
        else:
            interrupted = True
    except Exception as err:
        await menu_answer(message, f"Ошибка: {err}")
        return
    finally:
        await _free_slot(scrapes, pool, stop, status)

    if bad_input:
        await menu_answer(message, f"Неверные параметры: {bad_input}")
        return

    # The stale file was removed above, so its presence now means this run wrote it.
    wrote = os.path.exists(output)

    if wrote:
        await message.answer("Готово ✅")
        await _send_files(message.bot, message.chat.id, [output])
        await menu_answer(message, "Верификация завершена. Вы в главном меню.")
    elif interrupted and _user_stopped:
        await menu_answer(message, "⏹ Верификация остановлена.")
    elif interrupted:
        if not _shutting_down:  # else the bot is going down: nothing to tell
            await menu_answer(message, "Верификация прервана, повторите.")
    else:
        await menu_answer(message, "Готово ✅ (пропущенных постов не найдено).")


# the step each flow goes on to once its account is chosen (see _choose_account)
_FLOW_NEXT = {
    "scrape_resume": _resume_on_chosen,
    "project": _check_project_channels,
    "topup": _ask_go,
    "members": _ask_groups,
    "verify": _ask_posts_file,
}

# the step a flow goes on to from its chat list's ▶️ (see _check_chats); called as _FLOW_NEXT's steps are
_CHATS_NEXT = {
    Scrape.channels.state: _ask_date_min,
    Members.chats.state: _members_ready,
    Verify.channel.state: _ask_verify_dmin,
}


# =============================== analysis ===============================

def _links(path: str, data: dict):
    out = analysis.links(path, scraped_files.output_path(path, "links"))
    top = read_table(out).head(10)
    lines = [f"{row['Frequency']} — {row['Telegram Link']}" for _, row in top.iterrows()]
    return ("Чаще всего упоминаются:\n" + "\n".join(lines) if lines else "Ссылок на каналы не найдено."), out


# tool -> (button, what it does, input: "posts" | "any" | "combine", action(path, data) -> (text, file_or_None));
# every result lands next to its input file, named by scraped_files.output_path
ANALYSIS = {
    "links": (
        "🔗 Ссылки на другие каналы",
        "Найдёт в постах ссылки на Telegram-каналы и посчитает, сколько раз упоминался каждый. "
        "Так удобно искать похожие каналы для следующего скрапа.",
        "posts", _links,
    ),
    "filter": (
        "🔎 Поиск постов по словам",
        "Оставит только посты, где есть хотя бы одно из ваших слов, и пришлёт их таблицей.",
        "posts",
        lambda p, d: (analysis.filter_keywords(
            p, scraped_files.output_path(p, "keywords"), "Content", d["keywords"], 1_000_000), None),
    ),
    "read": (
        "👀 Посмотреть файл",
        "Покажет первые 10 строк любого файла из папки и его размер.",
        "any", None,  # special-cased: a preview, no file
    ),
    "combine": (
        "🧩 Объединить файлы постов",
        "Склеит несколько файлов постов в один и уберёт повторы. Результат, Combined_posts, "
        "можно выбрать в «Верификации» и в других инструментах.",
        "combine",
        lambda p, d: (None, analysis.combine(
            p, str(Path(scraped_files.BASES_DIR) / "Combined_posts"), ["Group", "Message ID"])),
    ),
    "comments": (
        "💬 Комментарии одной таблицей",
        "Развернёт комментарии под постами в таблицу: одна строка — один комментарий.",
        "posts",
        lambda p, d: (None, analysis.explode_comments(p, scraped_files.output_path(p, "comments"), "excel")),
    ),
    "summary": (
        "📊 Активность по месяцам",
        "Посчитает по месяцам, сколько было постов и комментариев в каждом канале.",
        "posts",
        lambda p, d: (analysis.summary(p, scraped_files.output_path(p, "summary"), "Date", "Group", "Comments"), None),
    ),
    "sample": (
        "🎲 Случайная выборка постов",
        "Выберет до 10 000 постов с текстом, пропорционально по каналам, для ручного просмотра.",
        "posts",
        lambda p, d: (None, analysis.sample(p, scraped_files.output_path(p, "sample"), "Content", "Group", 10000, 20)),
    ),
    "participants": (
        "👥 Пересобрать базу участников",
        "Заново соберёт базу людей для рассылки из файла постов (авторы, комментаторы, реакции "
        "из файла реакций рядом). Заменит базу этого скрапа — с тем же Owner ID.",
        "posts",
        # the scrape's own name: the rebuilt base replaces it for the mailing, as in the menu
        lambda p, d: (None, analysis.participants(p, scraped_files.output_path(p, "participants"),
                                                  None, "parquet")),
    ),
}
MAIN_TOOLS = ("links", "filter", "read")
MORE_TOOLS = ("combine", "comments", "summary", "sample", "participants")


def _tools_kb(tools, more: bool, token: str):
    """`token`: the project's menu (analysis_project) — its buttons act on that project only."""
    suffix = f".{token}"
    options = [(ANALYSIS[tool][0], tool + suffix) for tool in tools]
    if more:
        options.append(("➕ Ещё", "more" + suffix))
    return choice_kb("an_tool", options)


async def analysis_project(message: Message, state: FSMContext, project):
    """The analysis tools, from a project's card (bot/routers/projects.py): they offer its files only."""
    await state.clear()
    # its buttons carry the token, as a card's: an older menu's must not act on this one
    token = secrets.token_hex(4)
    await state.update_data(only_project=project.name, analysis_token=token)
    await message.answer(
        "📈 <b>Анализ данных</b> — утилиты для файлов скрапа. Аккаунты не нужны: бот читает файл и "
        "присылает результат.", parse_mode="HTML",
        reply_markup=_tools_kb(MAIN_TOOLS, more=True, token=token),
    )


@router.callback_query(ChoiceCB.filter(F.scope == "an_tool"))
async def analysis_tool(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext):
    await callback.answer()
    tool, _, token = callback_data.value.partition(".")
    data = await state.get_data()
    if token != data.get("analysis_token"):  # a project's menu shown before the last one
        await callback.message.answer(STALE_BUTTON)
        return
    if tool == "more":
        await callback.message.answer("Ещё инструменты:", reply_markup=_tools_kb(MORE_TOOLS, more=False, token=token))
        return
    if tool not in ANALYSIS:
        return

    label, hint, kind, _ = ANALYSIS[tool]
    project = data["only_project"]
    files = (scraped_files.project_files(project) if kind == "any"
             else scraped_files.project_files(project, kinds=("posts",)))
    options = [(f"📄 {name}", str(i)) for i, (_, name) in enumerate(files)]
    if kind == "combine":
        options = [(f"🗂 Все файлы постов ({len(files)})", "all")] if len(files) > 1 else []
        ask = ("Нажмите кнопку или пришлите путь к папке или шаблон "
               f"(например {scraped_files.BASES_DIR}/*_posts*.parquet):")
    elif files:
        ask = "Выберите файл или пришлите путь к нему:"
    elif kind == "any":
        ask = "У проекта нет файлов. Пришлите путь к файлу:"
    else:
        ask = "У проекта нет файлов с постами. Пришлите путь к файлу:"

    await state.set_state(Analysis.file)
    await state.update_data(tool=tool, files=[path for path, _ in files])
    await callback.message.answer(
        f"<b>{label}</b>\n{hint}\n\n{ask}", parse_mode="HTML",
        reply_markup=cancel_kb(Analysis.file, choice_kb("an_file", options) if options else None),
    )


@router.callback_query(Analysis.file, ChoiceCB.filter(F.scope == "an_file"))
async def analysis_file_pick(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext,
                             analyses: JobManager):
    await callback.answer()
    files = (await state.get_data()).get("files", [])
    if callback_data.value == "all":
        path = files
    else:
        index = int(callback_data.value)
        if not 0 <= index < len(files):
            return
        path = files[index]
    await _analysis_file_chosen(callback.message, state, analyses, path)


@router.message(Analysis.file)
async def analysis_file_text(message: Message, state: FSMContext, analyses: JobManager):
    path = await require_text(message)
    if path is None:
        return
    await _analysis_file_chosen(message, state, analyses, path)


async def _analysis_file_chosen(message: Message, state: FSMContext, analyses: JobManager, path):
    tool = (await state.get_data())["tool"]
    if tool == "filter":
        await state.update_data(path=path)
        await state.set_state(Analysis.keywords)
        await message.answer("Слова через запятую (например: крипта, биткоин):",
                             reply_markup=cancel_kb(Analysis.keywords))
        return
    await _clear_keeping_menu(state)
    await _run_analysis(message, analyses, tool, path, {})


@router.message(Analysis.keywords)
async def analysis_keywords(message: Message, state: FSMContext, analyses: JobManager):
    raw = await require_text(message)
    if raw is None:
        return
    keywords = [k.strip() for k in raw.split(",") if k.strip()]
    if not keywords:
        await message.answer("Нужно хотя бы одно слово. Слова через запятую:")
        return
    data = await state.get_data()
    await _clear_keeping_menu(state)
    await _run_analysis(message, analyses, data["tool"], data["path"], {"keywords": keywords})


async def _clear_keeping_menu(state: FSMContext):
    """The flow's step is over; a project's analysis menu (its token) keeps working on its project."""
    data = await state.get_data()
    await state.clear()
    if data.get("analysis_token"):
        await state.update_data(only_project=data["only_project"], analysis_token=data["analysis_token"])


def run_tool(tool: str, path, data: dict):
    """A tool's (text, file or None). Runs in a child process (see scraping.do_analysis):
    a giant file may take a lot of memory, which must not be the bot's."""
    if tool == "read":
        return _preview(path), None
    return ANALYSIS[tool][3](path, data)


def _preview(path) -> str:
    """read_preview of a file's first rows; a parquet file is not read whole for it."""
    if Path(path).suffix.lower() != ".parquet":
        return read_preview(read_table(path))
    posts = pq.ParquetFile(path)
    head = next(posts.iter_batches(batch_size=10), None)
    df = head.to_pandas() if head is not None else pd.DataFrame(columns=posts.schema_arrow.names)
    return read_preview(df, rows=posts.metadata.num_rows)


async def _run_analysis(message: Message, analyses: JobManager, tool: str, path, data: dict):
    # summary/filter write several files by a prefix and return no path; snapshot the
    # output dir (the input's) so the created files can be sent back, the way scrape
    # does (before acquire, as there).
    multi_file = tool in ("summary", "filter")
    out_dir = str(Path(path).parent) if multi_file else None
    try:
        before = scraping.dir_snapshot(out_dir) if multi_file else None
    except OSError as err:
        await message.answer(f"Папка вывода недоступна: {err}")
        return

    if not analyses.acquire("Анализ", cancelable=False, timeout=0):
        await message.answer(f"⛔ Занят: {analyses.label}. Дождитесь завершения.")
        return

    try:
        await message.answer("Выполняется…")
        text, file_path = await scraping.do_analysis(tool, path, data)
        if tool == "read":
            await message.answer(text, parse_mode="HTML")
            await menu_answer(message, "Готово ✅")
            return
    except (Exception, SystemExit) as err:  # scraper.analysis raises SystemExit on bad input
        await menu_answer(message, f"Ошибка: {err}")
        return
    finally:
        analyses.release()

    if isinstance(text, str) and text:  # summary returns None; links a top list
        await message.answer(text)

    if multi_file:
        files = scraping.new_files(out_dir, before)
        if files:
            await _send_files(message.bot, message.chat.id, files)
        await menu_answer(message, "Готово ✅" if files else "Готово, но ничего не найдено.")
        return

    if file_path and os.path.exists(file_path):
        await message.answer("Готово ✅")
        await _send_files(message.bot, message.chat.id, [file_path])
        await menu_answer(message, "Анализ завершён. Вы в главном меню.")
    else:
        await menu_answer(message, "Готово ✅")
