import asyncio
import html
import os
import threading
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message

from bot.callbacks import ChoiceCB, FunctionCB
from bot.keyboards.common import choice_kb, scrape_kb
from bot.keyboards.menu import main_menu
from bot.routers._common import require_text
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from bot.services import scraping
from bot.services.progress import Progress
from modules import json_file, scraped_files
from bot.states import Analysis, Members, Scrape, Verify
from modules.scraper_creds import build_credentials, find_account, scrape_accounts

from scraper import analysis
from scraper.datafiles import read_table
from scraper.members import MembersParams
from scraper.scrape import ScrapeParams, parse_channels, parse_date, pending_resume, resume_params
from scraper.verify import VerifyParams

router = Router()

DEFAULT_OUT = scraped_files.BASES_DIR


async def _menu(message: Message, text: str):
    """Send a terminal message and return the operator to the main menu."""
    await message.answer(text, reply_markup=main_menu())


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
_BASE_FLOWS = {"scrape", "scrape_resume", "members"}  # the flows that build a base
# a worker in a running bot job: one account must not scrape and mail at once (see WorkerPool)
WORKER_BUSY = ("⛔ Этот воркер сейчас занят задачей бота. Выберите другой аккаунт (например, личный) "
               "или дождитесь её окончания.")
PERSONAL_BUSY = "⛔ Бот сейчас читает коды входа этого аккаунта — повторите через несколько секунд."


def _accounts(pool: WorkerPool, personal) -> list:
    return scrape_accounts(pool.storage, personal)


async def _choose_account(message: Message, state: FSMContext, flow: str, **deps):
    """Ask which account the flow runs on; with one candidate it is taken at once.
    deps (pool, personal, scrapes) reach the flow's next step."""
    accounts = _accounts(deps["pool"], deps["personal"])
    if not accounts:
        await state.clear()
        await _menu(message, "Нет аккаунтов: добавьте воркера или личный аккаунт — 🤖 Воркеры → 📲 Добавить по номеру.")
        return
    if len(accounts) == 1:
        await _account_chosen(message, state, accounts[0], flow, deps)
        return
    await state.update_data(account_flow=flow, account_paths=[a.path for a in accounts])
    await message.answer("На каком аккаунте?", reply_markup=choice_kb("scr_acc", [
        (f"{'👤' if a.personal else '🤖'} {a.label}", str(i)) for i, a in enumerate(accounts)
    ]))


@router.callback_query(ChoiceCB.filter(F.scope == "scr_acc"))
async def account_pick(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext,
                       pool: WorkerPool, personal, scrapes: JobManager):
    await callback.answer()
    data = await state.get_data()
    paths = data.get("account_paths", [])
    index = int(callback_data.value)
    account = find_account(_accounts(pool, personal), paths[index]) if 0 <= index < len(paths) else None
    if account is None or "account_flow" not in data:  # a stale button
        await callback.message.answer("Флоу устарел, начните заново.")
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
    if pool.busy(account.path):  # a personal account: only while its login codes are read
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
    _disarm_stop(stop)
    scrapes.release()
    pool.scraping = None
    await _drop_button(status)


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


@router.callback_query(FunctionCB.filter(F.key == "scrape"))
async def scrape_start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool, personal):
    await callback.answer()
    if not _accounts(pool, personal):
        await callback.message.answer("Нет аккаунтов для скрапа (воркеров или личных).")
        return
    await state.clear()  # an abandoned run's keyword / limit / toggles would otherwise carry over
    await state.set_state(Scrape.name)
    await callback.message.answer("Имя для выходных файлов:")


@router.message(Scrape.name)
async def scrape_name(message: Message, state: FSMContext):
    name = await require_text(message)
    if name is None:
        return
    await state.update_data(name=name)
    await state.set_state(Scrape.out_dir)
    await message.answer(f"Папка вывода («-» = {DEFAULT_OUT}):")


@router.message(Scrape.out_dir)
async def scrape_out(message: Message, state: FSMContext, pool: WorkerPool, personal,
                     scrapes: JobManager):
    raw = await require_text(message)  # a sticker / photo must not fall back to the default folder
    if raw is None:
        return
    out_dir = DEFAULT_OUT if raw == "-" else raw
    await state.update_data(out_dir=out_dir)

    meta = pending_resume(out_dir, (await state.get_data())["name"])
    if meta is not None:  # an interrupted scrape under this name: offer to continue it
        await state.set_state(Scrape.resume)
        await message.answer(
            f"Здесь есть незавершённый скрап «{meta['name']}»:\n"
            f"Каналы: {', '.join(meta['channels'])}\n"
            f"Период: {meta['date_min'][:10]} – {meta['date_max'][:10]}\n"
            f"Уже собрано постов: {meta.get('t_index', 0)}",
            reply_markup=choice_kb("scrape_resume", [
                ("▶️ Продолжить", "continue"),
                ("🆕 Начать заново (собранное удалится)", "fresh"),
            ]),
        )
        return
    await _choose_account(message, state, "scrape", pool=pool, personal=personal, scrapes=scrapes)


@router.callback_query(Scrape.resume, ChoiceCB.filter(F.scope == "scrape_resume"))
async def scrape_resume(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext,
                        pool: WorkerPool, personal, scrapes: JobManager):
    await callback.answer()
    deps = dict(pool=pool, personal=personal, scrapes=scrapes)
    if callback_data.value == "fresh":
        await _choose_account(callback.message, state, "scrape", **deps)
        return
    data = await state.get_data()
    await state.clear()
    meta = pending_resume(data["out_dir"], data["name"])
    if meta is None:  # finished or removed meanwhile
        await _menu(callback.message, "Незавершённый скрап уже не найден.")
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
        await _menu(message, "Незавершённый скрап уже не найден.")
        return
    params = resume_params(meta, data["out_dir"])
    params.account = data["account"]
    await _run_scrape(message.bot, message.chat.id, deps["scrapes"], deps["pool"], deps["personal"], params)


async def _ask_channels(message: Message, state: FSMContext, deps: dict):
    await state.set_state(Scrape.channels)
    await message.answer(
        "Каналы/группы через запятую (@name / t.me ссылка / инвайт t.me/+… / числовой id).\n"
        "Одна тема форума — ссылкой на неё (t.me/name/42), личный чат — @username.\n"
        "Приватный канал/группа (инвайт, t.me/c/…, числовой id) — аккаунт должен в нём состоять."
    )


@router.message(Scrape.channels)
async def scrape_channels(message: Message, state: FSMContext):
    channels = await require_text(message)
    if channels is None:
        return
    await state.update_data(channels=channels)
    await state.set_state(Scrape.date_min)
    await message.answer("Дата с (ДД.ММ.ГГГГ или ГГГГ-ММ-ДД):")


@router.message(Scrape.date_min)
async def scrape_dmin(message: Message, state: FSMContext):
    date_min = await require_text(message)
    if date_min is None:
        return
    await state.update_data(date_min=date_min)
    await state.set_state(Scrape.date_max)
    await message.answer("Дата по (ДД.ММ.ГГГГ или ГГГГ-ММ-ДД):")


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
    await state.set_state(Scrape.confirm)
    await message.answer(
        f"Каналы: {', '.join(params.channels)}\n"
        f"Период: {data['date_min']} – {date_max}\n"
        f"Файлы: {data['out_dir']}/{data['name']}_…\n\n"
        "«Дополнительно» — комментарии, реакции, база участников, фильтр по слову, лимит постов.",
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
        (f"🔎 Фильтр по слову: {keyword[:24]}", "keyword"),
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
        await callback.message.answer("Фильтр по слову: только посты с ним («-» = без фильтра):")
    elif option == "max":
        await state.set_state(Scrape.max_messages)
        await callback.message.answer("Максимум постов (число, «-» = все):")
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
        await _menu(callback.message, f"Неверные параметры: {err}")
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
            await menu("⏹ Скрап остановлен. Собранное сохранено: запустите скрап с тем же именем и "
                       "папкой — бот предложит продолжить.")
        elif not _shutting_down:  # interrupted: the marker stays for a restart to continue it
            await menu("Скрап прерван (сеть / флуд-бан). Собранное сохранено: запустите скрап "
                       "с тем же именем и папкой — бот предложит продолжить.")
        return
    except Exception as err:  # e.g. the scrape's process killed (out of memory)
        _drop_marker()
        saved = (" Собранное сохранено: запустите скрап с тем же именем и папкой — бот предложит "
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
    # progress since the last automatic resume, by the checkpoint's post count
    t_index = meta.get("t_index", 0)
    stalls = marker.get("stalls", 0) + 1 if marker.get("t_index") == t_index else 0
    if stalls >= MAX_STALLED_RESUMES:
        _drop_marker()
        try:
            await bot.send_message(
                marker["chat_id"],
                f"⚠️ Автопродолжение скрапа «{marker['name']}» отключено: он падал при каждом запуске "
                "на одном месте (вероятно, не хватает памяти на финальную сборку). Собранное сохранено — "
                "продолжите вручную: скрап с тем же именем и папкой.")
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
    if _job_stop is not None:
        _job_stop.set()


# =============================== members ===============================

@router.callback_query(FunctionCB.filter(F.key == "members"))
async def members_start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool, personal,
                        scrapes: JobManager):
    await callback.answer()
    await state.clear()
    await _choose_account(callback.message, state, "members", pool=pool, personal=personal, scrapes=scrapes)


async def _ask_groups(message: Message, state: FSMContext, deps: dict):
    await state.set_state(Members.chats)
    await message.answer(
        "Группы через запятую (@name / t.me ссылка / инвайт t.me/+… / числовой id).\n"
        "Аккаунт должен состоять в группе. Подписчики канала доступны только админам."
    )


@router.message(Members.chats)
async def members_chats(message: Message, state: FSMContext):
    chats = await require_text(message)
    if chats is None:
        return
    if not parse_channels(chats):
        await message.answer("Не вижу ни одной группы. Пришлите ещё раз:")
        return
    await state.update_data(chats=chats)
    await state.set_state(Members.name)
    await message.answer("Имя для файла базы:")


@router.message(Members.name)
async def members_run(message: Message, state: FSMContext, pool: WorkerPool, personal,
                      scrapes: JobManager):
    name = await require_text(message)
    if name is None:
        return
    data = await state.get_data()
    await state.clear()

    account = _run_account(pool, personal, data.get("account", ""))
    if account is None:
        await _menu(message, "Аккаунт не найден, начните заново.")
        return

    progress = await _take_slot(scrapes, pool, account, "Участники чата",
                                lambda text: _menu(message, text), message.answer)
    if progress is None:
        return
    params = MembersParams(chats=parse_channels(data["chats"]), name=name,
                           out_dir=Path(DEFAULT_OUT), on_progress=progress.update)
    stop = _arm_stop(params)
    status = None
    try:
        status = await message.answer("Собираю участников…", reply_markup=scrape_kb())
        path, problems = await scraping.do_members(build_credentials(account.client), params)
    except (Exception, SystemExit) as err:  # connect() raises SystemExit on a logged-out session
        await _menu(message, f"Ошибка: {err}")
        return
    finally:
        await _free_slot(scrapes, pool, stop, status)

    if problems:
        await message.answer("Не всё удалось собрать:\n" + "\n".join(
            f"• {chat}: {problem}" for chat, problem in problems))
    if path is None:
        await _menu(message, "Ни одного участника не собрано.")
        return
    await _send_files(message.bot, message.chat.id, [str(path)])
    await _menu(message, "⏹ Остановлено. Собранные участники сохранены — база в списке баз для рассылки "
                         "и контактов." if _user_stopped else
                "Готово ✅ База — в списке баз для рассылки и контактов.")


# =============================== verify ===============================

@router.callback_query(FunctionCB.filter(F.key == "verify"))
async def verify_start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool, personal,
                       scrapes: JobManager):
    await callback.answer()
    await state.clear()
    await _choose_account(callback.message, state, "verify", pool=pool, personal=personal, scrapes=scrapes)


async def _ask_posts_file(message: Message, state: FSMContext, deps: dict):
    await state.set_state(Verify.input)
    files = scraped_files.posts_bases()
    await state.update_data(files=[path for path, _ in files])  # buttons carry an index into this
    if files:
        await message.answer(
            "Выберите файл с постами или пришлите путь к файлу/папке:",
            reply_markup=choice_kb("verify_base", [(label, str(i)) for i, (_, label) in enumerate(files)]),
        )
    else:
        await message.answer(
            f"В {scraped_files.BASES_DIR} нет файлов с постами (*_posts). Пришлите путь к файлу или папке:"
        )


def _ddmmyyyy(iso_date: str) -> str:
    year, month, day = iso_date.split("-")
    return f"{day}.{month}.{year}"


async def _ask_channel(message: Message, state: FSMContext, input_: str):
    """Offer the posts file's channels as buttons; with the scrape's window known, a
    button runs the verify at once."""
    channels, window = await asyncio.to_thread(scraped_files.verify_presets, input_)
    await state.update_data(input=input_, channels=channels, window=window)
    await state.set_state(Verify.channel)

    if not channels:
        await message.answer("Канал (@name / t.me / числовой id):")
        return

    if window:
        span = f"{_ddmmyyyy(window[0])}–{_ddmmyyyy(window[1])}"
        text = (f"Каналы из файла — кнопка сразу запустит проверку за период скрапа ({span}).\n"
                "Другой канал или другие даты — введите канал текстом:")
        labels = [f"▶️ {channel} · {span}" for channel in channels]
    else:
        text = "Канал из файла — или введите другой (@name / t.me / числовой id):"
        labels = channels
    await message.answer(
        text, reply_markup=choice_kb("verify_channel", [(label, str(i)) for i, label in enumerate(labels)])
    )


@router.callback_query(Verify.input, ChoiceCB.filter(F.scope == "verify_base"))
async def verify_base(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext):
    await callback.answer()
    files = (await state.get_data()).get("files", [])
    index = int(callback_data.value)
    if not 0 <= index < len(files):
        return
    await _ask_channel(callback.message, state, files[index])


@router.message(Verify.input)
async def verify_input(message: Message, state: FSMContext):
    input_ = await require_text(message)
    if input_ is None:
        return
    await _ask_channel(message, state, input_)


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
    await callback.message.answer("Дата с:")


@router.message(Verify.channel)
async def verify_channel(message: Message, state: FSMContext):
    channel = await require_text(message)
    if channel is None:
        return
    await state.update_data(channel=channel)
    await state.set_state(Verify.date_min)
    await message.answer("Дата с:")


@router.message(Verify.date_min)
async def verify_dmin(message: Message, state: FSMContext):
    date_min = await require_text(message)
    if date_min is None:
        return
    await state.update_data(date_min=date_min)
    await state.set_state(Verify.date_max)
    await message.answer("Дата по:")


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
        await _menu(message, "Аккаунт не найден, начните заново.")
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
                                lambda text: _menu(message, text), message.answer)
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
        await _menu(message, f"Ошибка: {err}")
        return
    finally:
        await _free_slot(scrapes, pool, stop, status)

    if bad_input:
        await _menu(message, f"Неверные параметры: {bad_input}")
        return

    # The stale file was removed above, so its presence now means this run wrote it.
    wrote = os.path.exists(output)

    if wrote:
        await message.answer("Готово ✅")
        await _send_files(message.bot, message.chat.id, [output])
        await _menu(message, "Верификация завершена. Вы в главном меню.")
    elif interrupted and _user_stopped:
        await _menu(message, "⏹ Верификация остановлена.")
    elif interrupted:
        if not _shutting_down:  # else the bot is going down: nothing to tell
            await _menu(message, "Верификация прервана, повторите.")
    else:
        await _menu(message, "Готово ✅ (пропущенных постов не найдено).")


# the step each flow goes on to once its account is chosen (see _choose_account)
_FLOW_NEXT = {
    "scrape": _ask_channels,
    "scrape_resume": _resume_on_chosen,
    "members": _ask_groups,
    "verify": _ask_posts_file,
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


def _tools_kb(tools, more: bool):
    options = [(ANALYSIS[tool][0], tool) for tool in tools]
    if more:
        options.append(("➕ Ещё", "more"))
    return choice_kb("an_tool", options)


@router.callback_query(FunctionCB.filter(F.key == "analysis"))
async def analysis_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.answer(
        "Утилиты для файлов скрапа. Аккаунты не нужны: бот читает файл и присылает результат.",
        reply_markup=_tools_kb(MAIN_TOOLS, more=True),
    )


@router.callback_query(ChoiceCB.filter(F.scope == "an_tool"))
async def analysis_tool(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext):
    await callback.answer()
    tool = callback_data.value
    if tool == "more":
        await callback.message.answer("Ещё инструменты:", reply_markup=_tools_kb(MORE_TOOLS, more=False))
        return
    if tool not in ANALYSIS:
        return

    label, hint, kind, _ = ANALYSIS[tool]
    files = scraped_files.data_files() if kind == "any" else scraped_files.posts_bases(limit=100 if kind == "combine" else 10)
    options = [(name, str(i)) for i, (_, name) in enumerate(files)]
    if kind == "combine":
        options = [(f"Все файлы постов ({len(files)})", "all")] if len(files) > 1 else []
        ask = ("Нажмите кнопку или пришлите путь к папке или шаблон "
               f"(например {scraped_files.BASES_DIR}/*_posts*.parquet):")
    elif files:
        ask = "Выберите файл или пришлите путь к нему:"
    elif kind == "any":
        ask = f"В {scraped_files.BASES_DIR} нет файлов. Пришлите путь к файлу:"
    else:
        ask = f"В {scraped_files.BASES_DIR} нет файлов с постами (*_posts). Пришлите путь к файлу:"

    await state.set_state(Analysis.file)
    await state.update_data(tool=tool, files=[path for path, _ in files])
    await callback.message.answer(
        f"<b>{label}</b>\n{hint}\n\n{ask}", parse_mode="HTML",
        reply_markup=choice_kb("an_file", options) if options else None,
    )


@router.callback_query(Analysis.file, ChoiceCB.filter(F.scope == "an_file"))
async def analysis_file_pick(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext,
                             manager: JobManager):
    await callback.answer()
    files = (await state.get_data()).get("files", [])
    if callback_data.value == "all":
        path = files
    else:
        index = int(callback_data.value)
        if not 0 <= index < len(files):
            return
        path = files[index]
    await _analysis_file_chosen(callback.message, state, manager, path)


@router.message(Analysis.file)
async def analysis_file_text(message: Message, state: FSMContext, manager: JobManager):
    path = await require_text(message)
    if path is None:
        return
    await _analysis_file_chosen(message, state, manager, path)


async def _analysis_file_chosen(message: Message, state: FSMContext, manager: JobManager, path):
    tool = (await state.get_data())["tool"]
    if tool == "filter":
        await state.update_data(path=path)
        await state.set_state(Analysis.keywords)
        await message.answer("Слова через запятую (например: крипта, биткоин):")
        return
    await state.clear()
    await _run_analysis(message, manager, tool, path, {})


@router.message(Analysis.keywords)
async def analysis_keywords(message: Message, state: FSMContext, manager: JobManager):
    raw = await require_text(message)
    if raw is None:
        return
    keywords = [k.strip() for k in raw.split(",") if k.strip()]
    if not keywords:
        await message.answer("Нужно хотя бы одно слово. Слова через запятую:")
        return
    data = await state.get_data()
    await state.clear()
    await _run_analysis(message, manager, data["tool"], data["path"], {"keywords": keywords})


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


async def _run_analysis(message: Message, manager: JobManager, tool: str, path, data: dict):
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

    if not manager.acquire("Анализ", cancelable=False, timeout=0):
        await message.answer(f"⛔ Занят: {manager.label}. Дождитесь завершения.")
        return

    try:
        await message.answer("Выполняется…")
        text, file_path = await scraping.do_analysis(tool, path, data)
        if tool == "read":
            await message.answer(text, parse_mode="HTML")
            await _menu(message, "Готово ✅")
            return
    except (Exception, SystemExit) as err:  # scraper.analysis raises SystemExit on bad input
        await _menu(message, f"Ошибка: {err}")
        return
    finally:
        manager.release()

    if isinstance(text, str) and text:  # summary returns None; links a top list
        await message.answer(text)

    if multi_file:
        files = scraping.new_files(out_dir, before)
        if files:
            await _send_files(message.bot, message.chat.id, files)
        await _menu(message, "Готово ✅" if files else "Готово, но ничего не найдено.")
        return

    if file_path and os.path.exists(file_path):
        await message.answer("Готово ✅")
        await _send_files(message.bot, message.chat.id, [file_path])
        await _menu(message, "Анализ завершён. Вы в главном меню.")
    else:
        await _menu(message, "Готово ✅")
