"""The check of a project's posts file against the live channel (bot/routers/projects/card.py)."""
import asyncio
import os

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import ChoiceCB
from bot.keyboards.common import cancel_kb, choice_kb, page_of, paged_kb, scrape_kb
from bot.routers._common import pick_from, require_text, send_chunked, stale_tap
from bot.routers.scraping import common
from bot.services import scraping
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from bot.states import Verify
from modules import projects, scraped_files
from modules.registry import BOT_FUNCTIONS_BY_KEY
from modules.scraper_creds import build_credentials
from scraper.scrape import (
    bad_channels, bad_channels_text, chat_block, parse_date,
)
from scraper.verify import VerifyParams

router = Router()


async def verify_project(message: Message, state: FSMContext, project, deps: dict):
    """The verify from a project's 📥 Сбор (bot/routers/projects/card.py): its posts files only."""
    await state.clear()
    await state.update_data(only_project=project.name)
    await common.choose_account(message, state, "verify", **deps)


async def _ask_posts_file(message: Message, state: FSMContext, deps: dict):
    await state.set_state(Verify.input)
    files = scraped_files.project_files((await state.get_data())["only_project"], kinds=("posts",))
    await state.update_data(files=[path for path, _ in files])  # buttons carry an index into this
    if files:
        await message.answer(
            "✅ Верификация — выберите файл с постами или пришлите путь к файлу или папке на сервере:",
            reply_markup=cancel_kb(Verify.input, choice_kb(
                "verify_base", [(f"📄 {label}", str(i)) for i, (_, label) in enumerate(files)])),
        )
    else:
        await message.answer(
            "✅ Верификация — у проекта нет файлов с постами. Пришлите путь к файлу или папке на сервере:",
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
        await message.answer("Пришлите канал (@name / t.me / числовой id):", reply_markup=cancel_kb(Verify.channel))
        return

    data = await state.get_data()
    infos = await common.lookup(message, pool, personal, data.get("account", ""), channels)
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
        # why, numbered in the buttons' order; on their own: a hundred of them would not fit with the prompt
        await send_chunked(message, "", [chat_block(i, info, html=True) for i, info in enumerate(infos, 1)
                                         if info.problem])
        text += "\n\n⚠️ — канал сейчас не читается, почему — выше."
    await state.update_data(channel_labels=labels)  # a page of the buttons is drawn again from them
    await message.answer(text, parse_mode="HTML", reply_markup=_channels_kb(labels, 0))


def _channels_kb(labels: list[str], page: int):
    """The file's channels, a page of them (keyboards.paged_kb): a big project's don't fit under one message."""
    return cancel_kb(Verify.channel, paged_kb("verify_channel", [(label, str(i)) for i, label in enumerate(labels)],
                                              page))


@router.callback_query(Verify.input, ChoiceCB.filter(F.scope == "verify_base"))
async def verify_base(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext,
                      pool: WorkerPool, personal):
    path = pick_from((await state.get_data()).get("files", []), callback_data.value)
    if path is None:  # a button of an older list of the files
        await stale_tap(callback)
        return
    await callback.answer()
    await _ask_channel(callback.message, state, pool, personal, path)


@router.message(Verify.input)
async def verify_input(message: Message, state: FSMContext, pool: WorkerPool, personal):
    input_ = await require_text(message)
    if input_ is None:
        return
    await _ask_channel(message, state, pool, personal, input_)


@router.callback_query(Verify.channel, ChoiceCB.filter(F.scope == "verify_channel"))
async def verify_channel_pick(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext,
                              pool: WorkerPool, personal, scrapes: JobManager):
    data = await state.get_data()
    if (page := page_of(callback_data.value)) is not None:  # ◀️ · 📄 · ▶️: another page of the channels
        await callback.answer()
        await callback.message.edit_reply_markup(reply_markup=_channels_kb(data.get("channel_labels", []), page))
        return
    channel = pick_from(data.get("channels", []), callback_data.value)
    if channel is None:  # a button of an older list of the channels
        await stale_tap(callback)
        return
    await callback.answer()

    window = data.get("window")
    if window:
        await state.clear()
        await _run_verify(callback.message, scrapes, pool, common.run_account(pool, personal, data.get("account", "")),
                          data["input"], channel, *window)
        return

    await state.update_data(channel=channel)
    await _ask_verify_dmin(callback.message, state)


@router.message(Verify.channel)
async def verify_channel(message: Message, state: FSMContext, pool: WorkerPool, personal):
    channel = await require_text(message)
    if channel is None:
        return
    if bad_channels([channel]):
        await message.answer(f"{bad_channels_text([channel])} Пришлите канал ещё раз:")
        return
    await state.update_data(channel=channel)
    project = (await state.get_data()).get("only_project")
    if await common.check_chats(message, state, pool, personal, [channel], project):
        await _ask_verify_dmin(message, state)


async def _ask_verify_dmin(message: Message, state: FSMContext, deps: dict | None = None):
    await state.set_state(Verify.date_min)
    await message.answer(common.DATE_MIN_ASK, reply_markup=cancel_kb(Verify.date_min))


@router.message(Verify.date_min)
async def verify_dmin(message: Message, state: FSMContext):
    date_min = await require_text(message)
    if not await common.date_or_ask(message, date_min):
        return
    await state.update_data(date_min=date_min)
    await state.set_state(Verify.date_max)
    await message.answer(common.DATE_MAX_ASK, reply_markup=cancel_kb(Verify.date_max))


@router.message(Verify.date_max)
async def verify_run(message: Message, state: FSMContext, pool: WorkerPool, personal,
                     scrapes: JobManager):
    date_max = await require_text(message)
    if not await common.date_or_ask(message, date_max, end_of_day=True):
        return

    data = await state.get_data()
    if parse_date(data["date_min"]) > parse_date(date_max, end_of_day=True):  # the step stays, as the scrape's
        await message.answer("Дата «с» позже даты «по». Пришлите дату «по» ещё раз:")
        return
    await state.clear()
    await _run_verify(message, scrapes, pool, common.run_account(pool, personal, data.get("account", "")),
                      data["input"], data["channel"], data["date_min"], date_max)


async def _run_verify(message: Message, scrapes: JobManager, pool: WorkerPool, account,
                      input_: str, channel: str, date_min: str, date_max: str):
    if account is None:
        await common.to_collect(message, "Аккаунт не найден, начните заново.", scrapes)
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
        await common.to_collect(message, f"Неверные параметры: {err}", scrapes)
        return

    if params.date_min > params.date_max:
        await common.to_collect(message, "Дата «с» позже даты «по».", scrapes)
        return

    progress = await common.take_slot(scrapes, pool, account, BOT_FUNCTIONS_BY_KEY["verify"].title,
                                      lambda text: common.to_collect(message, text, scrapes))
    if progress is None:
        return
    params.on_progress = progress.update
    stop = params.stop = scrapes.soft_stop()  # the job's own ⏹: a later job's can't relabel this one
    status = None
    interrupted = False
    bad_input = None
    try:
        status = await message.answer("Верификация запущена…", reply_markup=scrape_kb(scrapes.key))

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
        await common.to_collect(message, f"⚠️ Ошибка: {err}", scrapes)
        return
    finally:
        await common.free_slot(scrapes, pool, status)

    if bad_input:
        await common.to_collect(message, f"Неверные параметры: {bad_input}", scrapes)
        return

    # The stale file was removed above, so its presence now means this run wrote it.
    wrote = os.path.exists(output)

    if wrote:
        await common.send_files(message.bot, message.chat.id, [output])
        await common.to_collect(message, "✅ Верификация завершена: пропущенные посты — в файле выше.", scrapes)
    elif interrupted and stop.is_set() and not common.shutting_down:
        await common.to_collect(message, "⏹ Верификация остановлена.", scrapes)
    elif interrupted:
        if not common.shutting_down:  # else the bot is going down: nothing to tell
            await common.to_collect(message, "Верификация прервана, повторите.", scrapes)
    else:
        await common.to_collect(message, "✅ Верификация завершена: пропущенных постов не найдено.", scrapes)


common.FLOW_NEXT["verify"] = _ask_posts_file
common.CHATS_NEXT[Verify.channel.state] = _ask_verify_dmin
