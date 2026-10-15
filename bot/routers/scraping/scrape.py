"""A project's scrape and top-up in the bot (bot/routers/projects.py), continued after a bot restart."""
import asyncio
import html
import os
from pathlib import Path

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import ChoiceCB
from bot.keyboards.common import cancel_kb, choice_kb, scrape_kb
from bot.keyboards.menu import main_menu
from bot.routers._common import menu_answer, require_text, typed_channels
from bot.routers.scraping import common
from bot.services import scraping
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from bot.states import Scrape
from modules import json_file, projects, scraped_files
from modules.scraper_creds import build_credentials
from scraper.scrape import (
    ScrapeParams, parse_channels, parse_date, pending_resume, resume_params,
)

router = Router()


# A bot scrape in progress (scraped_files.SCRAPE_MARKER), continued automatically after a bot
# restart (see resume_after_restart): kept on an interruption or a shutdown, dropped once the
# scrape ends for good (finished, stopped by the operator, bad input).
# automatic resumes in a row that made no progress before auto-resume gives up: a run killed
# at the same spot each time (e.g. out of memory in its final step) must not crash-loop the bot
MAX_STALLED_RESUMES = 2


_resume_tasks: set = set()  # resume_after_restart's runs, kept referenced (the loop's weak ref lets GC drop them)


# A project scrapes one at a time: its marker is under its folder (pool.storage.home), in one file
# with the other projects' (scraped_files.scrape_markers).

def _write_marker(key: str, marker: dict):
    json_file.save(scraped_files.SCRAPE_MARKER, {**scraped_files.scrape_markers(), key: marker})


def _read_marker(key: str) -> dict | None:
    return scraped_files.scrape_markers().get(key)


def _drop_marker(key: str):
    markers = scraped_files.scrape_markers()
    if markers.pop(key, None) is None:
        return
    if markers:
        json_file.save(scraped_files.SCRAPE_MARKER, markers)
    else:
        try:
            os.remove(scraped_files.SCRAPE_MARKER)
        except OSError:
            pass


async def scrape_project(message: Message, state: FSMContext, project, deps: dict):
    """A project's scrape (bot/routers/projects.py): its name and channels are the project's,
    the channels are named on the chosen account as if typed, the rest is asked as usual."""
    await state.clear()
    await state.update_data(name=project.name, out_dir=common.DEFAULT_OUT, channels=", ".join(project.channels))
    await _scrape_named(message, state, deps, "project")


async def topup_project(message: Message, state: FSMContext, project, plan, deps: dict):
    """A project's top-up (bot/routers/projects.py): the previous scrape's channels, keyword and
    dates come from its posts file (modules/projects.topup); the account is asked, then the summary."""
    if pending_resume(common.DEFAULT_OUT, project.name) is not None:  # a fresh run would drop its checkpoint
        await menu_answer(message, "У проекта незавершённый скрап — сначала продолжите его: «▶️ Скрап проекта».")
        return
    await state.clear()
    await state.update_data(name=project.name, out_dir=common.DEFAULT_OUT, channels=", ".join(plan.channels),
                            date_min=f"{plan.date_min:%d.%m.%Y}", date_max=f"{plan.date_max:%d.%m.%Y}",
                            keyword=plan.keyword, extend=plan.posts)
    await common.choose_account(message, state, "topup", **deps)


async def _scrape_named(message: Message, state: FSMContext, deps: dict, flow: str):
    """The scrape's name is set: offer to continue an interrupted scrape under it, else ask the account."""
    data = await state.get_data()
    meta = pending_resume(common.DEFAULT_OUT, data["name"])
    if meta is not None:  # an interrupted scrape under this name: offer to continue it
        # a fresh scrape reads the project's channels: with none (all removed since), only the continuing
        fresh = bool(parse_channels(data["channels"]))
        await state.set_state(Scrape.resume)
        await message.answer(
            f"Под этим именем есть незавершённый скрап «{html.escape(meta['name'])}».\n\n"
            f"Каналы:\n\n{projects.chat_list(projects.find(data['name']), meta['channels'], html=True)}\n\n"
            f"Период: {meta['date_min'][:10]} – {meta['date_max'][:10]}\n"
            f"Уже собрано постов: {meta.get('t_index', 0)}"
            + ("" if fresh else f"\n\n🆕 Начать заново нельзя. {projects.NO_CHANNELS_TEXT}"),
            parse_mode="HTML",
            reply_markup=choice_kb("scrape_resume", [
                ("▶️ Продолжить", "continue"),
                *([("🆕 Начать заново (собранное удалится)", "fresh")] if fresh else []),
            ]),
        )
        return
    await common.choose_account(message, state, flow, **deps)


@router.callback_query(Scrape.resume, ChoiceCB.filter(F.scope == "scrape_resume"))
async def scrape_resume(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext,
                        pool: WorkerPool, personal, scrapes: JobManager):
    await callback.answer()
    deps = dict(pool=pool, personal=personal, scrapes=scrapes)
    data = await state.get_data()
    if callback_data.value == "fresh":  # a project's scrape keeps its channels
        await common.choose_account(callback.message, state, "project", **deps)
        return
    await state.clear()
    meta = pending_resume(data["out_dir"], data["name"])
    if meta is None:  # finished or removed meanwhile
        await menu_answer(callback.message, "Незавершённый скрап уже не найден.")
        return
    params = resume_params(meta, data["out_dir"])
    if not params.account:  # a checkpoint from before accounts were stored: ask, then continue
        await state.update_data(**data)
        await common.choose_account(callback.message, state, "scrape_resume", **deps)
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
    raw = await require_text(message)
    if raw is None:
        return
    channels = await typed_channels(message, raw)
    if channels is None:
        return
    await state.update_data(channels=raw)
    # a list corrected for this run: those of the project's own channels in it get named on its card
    if await common.check_chats(message, state, pool, personal, channels, (await state.get_data()).get("name")):
        await _ask_date_min(message, state)


async def _check_project_channels(message: Message, state: FSMContext, deps: dict):
    """A project's scrape: its channels are checked on the chosen account as if just typed (a
    corrected list sent now is for this run only)."""
    data = await state.get_data()
    if not parse_channels(data["channels"]):  # an old «🆕 Начать заново», or the checkpoint gone meanwhile
        await state.clear()
        await menu_answer(message, projects.NO_CHANNELS_TEXT)
        return
    await state.set_state(Scrape.channels)
    if await common.check_chats(message, state, deps["pool"], deps["personal"], parse_channels(data["channels"]),
                          data.get("name")):
        await _ask_date_min(message, state)


async def _ask_date_min(message: Message, state: FSMContext, deps: dict | None = None):
    await state.set_state(Scrape.date_min)
    await message.answer("Дата с (ДД.ММ.ГГГГ или ГГГГ-ММ-ДД):", reply_markup=cancel_kb(Scrape.date_min))


@router.message(Scrape.date_min)
async def scrape_dmin(message: Message, state: FSMContext):
    date_min = await require_text(message)
    if not await common.date_or_ask(message, date_min):
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


async def _run_scrape(bot, chat_id: int, scrapes: JobManager, pool: WorkerPool, personal,
                      params: ScrapeParams, resumes: dict | None = None):
    """resumes: the automatic-resume record kept in the marker (see resume_after_restart)."""
    async def menu(text):
        await bot.send_message(chat_id, text, reply_markup=main_menu())

    account = common.run_account(pool, personal, params.account)
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

    progress = await common.take_slot(scrapes, pool, account, "Скрап", menu,
                                lambda text: bot.send_message(chat_id, text))
    if progress is None:
        return
    params.on_progress = lambda frac, eta, posts: progress.set(frac, eta, f"Постов: {posts}")
    stop = params.stop = scrapes.soft_stop()
    status = None
    try:  # from here on, any error frees the slot and the worker (see finally)
        _write_marker(pool.storage.home, {"out_dir": out_dir, "name": params.name, "account": params.account,
                                          "chat_id": chat_id, **(resumes or {})})
        status = await bot.send_message(
            chat_id, f"Скрап запущен на аккаунте {account.label} — может занять долго.\n"
                     f"Остальные функции бота доступны{'' if account.personal else ' — без этого воркера'}.",
            reply_markup=scrape_kb(scrapes.key))
        await scraping.do_scrape(build_credentials(account.client), params)
    except SystemExit as err:
        # a str code is a user-input error with its reason; an int one an interrupted run,
        # which exits before any output file is written
        if isinstance(err.code, str) and err.code:
            _drop_marker(pool.storage.home)
            await menu(f"Скрап остановлен: {err}")
        elif stop.is_set() and not common.shutting_down:  # the operator's ⏹
            _drop_marker(pool.storage.home)
            await menu("⏹ Скрап остановлен. Собранное сохранено: запустите скрап с тем же именем "
                       "— бот предложит продолжить.")
        elif not common.shutting_down:  # interrupted: the marker stays for a restart to continue it
            await menu("Скрап прерван (сеть / флуд-бан). Собранное сохранено: запустите скрап "
                       "с тем же именем — бот предложит продолжить.")
        return
    except Exception as err:  # e.g. the scrape's process killed (out of memory)
        _drop_marker(pool.storage.home)
        saved = (" Собранное сохранено: запустите скрап с тем же именем — бот предложит "
                 "продолжить.") if pending_resume(out_dir, params.name) else ""
        await menu(f"Ошибка скрапа: {err}.{saved}")
        return
    finally:
        await common.free_slot(scrapes, pool, status)

    _drop_marker(pool.storage.home)
    files = scraping.new_files(out_dir, before, tuple(f"{params.name}{marker}"
                                                      for marker in ("_posts", "_reactors", "_participants")))
    if files:
        await bot.send_message(chat_id, f"Готово ✅ Файлов: {len(files)}")
        await common.send_files(bot, chat_id, files)
        await menu("Скрап завершён. Вы в главном меню.")
    else:
        await menu("Готово, но новых файлов не найдено.")


async def resume_after_restart(bot, workspaces, personal):
    """Bot startup: continue the scrapes a restart interrupted, each in its project (see
    scraped_files.SCRAPE_MARKER)."""
    for workspace in workspaces.all():
        await _resume(bot, workspace.scrapes, workspace.pool, personal)


async def _resume(bot, scrapes: JobManager, pool: WorkerPool, personal):
    key = pool.storage.home
    marker = _read_marker(key)
    if marker is None:
        return
    meta = pending_resume(marker["out_dir"], marker["name"])
    if meta is None:  # finished or removed meanwhile
        _drop_marker(key)
        return
    # progress since the last automatic resume, by the checkpoint's post count; a clean
    # shutdown (systemd / needrestart restarts) is no crash and doesn't count as a stall
    t_index = meta.get("t_index", 0)
    clean = marker.pop("clean_shutdown", False)
    stalls = marker.get("stalls", 0) + (not clean) if marker.get("t_index") == t_index else 0
    if stalls >= MAX_STALLED_RESUMES:
        _drop_marker(key)
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
    if common.run_account(pool, personal, params.account) is None:
        _drop_marker(key)  # its account is gone for good: _run_scrape says so once, no retries
    else:  # counted now: a run that returns before writing its own marker must still count
        _write_marker(key, {**marker, "t_index": t_index, "stalls": stalls})

    async def run():
        try:
            await bot.send_message(marker["chat_id"], f"🔄 Бот перезапущен — продолжаю скрап «{params.name}».")
        except Exception:
            pass
        await _run_scrape(bot, marker["chat_id"], scrapes, pool, personal, params,
                          resumes={"t_index": t_index, "stalls": stalls})

    # don't hold up the bot's start
    task = asyncio.create_task(run())
    _resume_tasks.add(task)
    task.add_done_callback(_resume_tasks.discard)


async def stop_for_shutdown(workspaces):
    """Bot shutdown: stop every project's scraper slot's job; a scrape checkpoints and its marker
    stays, so the next start continues it."""
    common.shutting_down = True
    for key, marker in scraped_files.scrape_markers().items():  # see _resume: a crash never gets here
        _write_marker(key, {**marker, "clean_shutdown": True})
    stopping = [workspace.scrapes for workspace in workspaces.all() if await workspace.scrapes.stop()]
    # their last bot call (common.free_slot) must come before aiogram closes the bot's session, or
    # it opens a new one nobody closes; within systemd's TimeoutStopSec (30 s)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + scraping.CHILD_STOP_GRACE + 5
    while any(scrapes.active for scrapes in stopping) and loop.time() < deadline:
        await asyncio.sleep(0.1)


common.FLOW_NEXT.update(scrape_resume=_resume_on_chosen, project=_check_project_channels, topup=_ask_go)
common.CHATS_NEXT[Scrape.channels.state] = _ask_date_min
