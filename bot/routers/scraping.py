import asyncio
import html
import os
from pathlib import Path

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import ChoiceCB, FunctionCB
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from bot.services import scraping
from bot.states import Analysis, Scrape, Verify
from modules.scraper_creds import build_credentials
from modules.settings import Settings

from scraper import analysis
from scraper.datafiles import read_table
from scraper.scrape import ScrapeParams, parse_date
from scraper.verify import VerifyParams

router = Router()

DEFAULT_OUT = "assets/databases"


def read_preview(df) -> str:
    """HTML-safe <pre> preview of a dataframe head for Telegram (escaped + length-capped)."""
    text = html.escape(df.head(10).to_string())
    if len(text) > 3500:
        text = text[:3500] + "…"
    return f"<pre>{text}</pre>\n[{len(df)} строк x {len(df.columns)} столбцов]"


async def _send_files(bot, chat_id, paths):
    for path in paths:
        if os.path.getsize(path) <= scraping.TELEGRAM_UPLOAD_LIMIT:
            await bot.send_document(chat_id, FSInputFile(path))
        else:
            await bot.send_message(chat_id, f"Файл слишком большой для Telegram, лежит на диске: {path}")


# =============================== scrape ===============================

@router.callback_query(FunctionCB.filter(F.key == "scrape"))
async def scrape_start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool):
    await callback.answer()
    if pool.count() == 0:
        await callback.message.answer("Нет воркер-аккаунтов (нужен аккаунт для скрапа).")
        return
    await state.set_state(Scrape.channels)
    await callback.message.answer("Каналы/группы через запятую:")


@router.message(Scrape.channels)
async def scrape_channels(message: Message, state: FSMContext):
    await state.update_data(channels=message.text.strip())
    await state.set_state(Scrape.name)
    await message.answer("Имя для выходных файлов:")


@router.message(Scrape.name)
async def scrape_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await state.set_state(Scrape.out_dir)
    await message.answer(f"Папка вывода (пусто = {DEFAULT_OUT}):")


@router.message(Scrape.out_dir)
async def scrape_out(message: Message, state: FSMContext):
    await state.update_data(out_dir=message.text.strip() or DEFAULT_OUT)
    await state.set_state(Scrape.date_min)
    await message.answer("Дата с (DD.MM.YYYY или YYYY-MM-DD):")


@router.message(Scrape.date_min)
async def scrape_dmin(message: Message, state: FSMContext):
    await state.update_data(date_min=message.text.strip())
    await state.set_state(Scrape.date_max)
    await message.answer("Дата по (DD.MM.YYYY или YYYY-MM-DD):")


@router.message(Scrape.date_max)
async def scrape_run(message: Message, state: FSMContext, pool: WorkerPool, manager: JobManager, settings: Settings):
    data = await state.get_data()
    await state.clear()

    session_string = scraping.worker_session_string(pool)
    if session_string is None:
        await message.answer("Нет воркеров.")
        return

    out_dir = data["out_dir"]
    try:
        params = ScrapeParams(
            channels=scraping.parse_channels(data["channels"]),
            date_min=parse_date(data["date_min"]),
            date_max=parse_date(message.text.strip(), end_of_day=True),
            name=data["name"],
            keyword="",
            max_messages=1_000_000,
            fmt="parquet",
            out_dir=Path(out_dir),
            with_comments=True,
            with_reactors=True,
            with_participants=True,
            resume=False,
        )
    except Exception as err:
        await message.answer(f"Неверные параметры: {err}")
        return

    if not manager.acquire("Скрап", cancelable=False, timeout=0):
        await message.answer(f"⛔ Занят: {manager.label}. Дождитесь завершения.")
        return

    before = scraping.dir_snapshot(out_dir)
    await message.answer("Скрап запущен — может занять долго. Дождитесь файлов.")

    try:
        await scraping.do_scrape(build_credentials(settings, session_string), params)
    except SystemExit as err:
        await message.answer(f"Скрап остановлен: {err}")
    except Exception as err:
        await message.answer(f"Ошибка скрапа: {err}")
    finally:
        manager.release()

    files = scraping.new_files(out_dir, before)
    if files:
        await message.answer(f"Готово ✅ Файлов: {len(files)}")
        await _send_files(message.bot, message.chat.id, files)
    else:
        await message.answer("Готово, но новых файлов не найдено.")


# =============================== verify ===============================

@router.callback_query(FunctionCB.filter(F.key == "verify"))
async def verify_start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool):
    await callback.answer()
    if pool.count() == 0:
        await callback.message.answer("Нет воркер-аккаунтов (нужен аккаунт).")
        return
    await state.set_state(Verify.input)
    await callback.message.answer("Файл со скрапнутыми постами:")


@router.message(Verify.input)
async def verify_input(message: Message, state: FSMContext):
    await state.update_data(input=message.text.strip())
    await state.set_state(Verify.channel)
    await message.answer("Канал (@name / t.me / numeric id):")


@router.message(Verify.channel)
async def verify_channel(message: Message, state: FSMContext):
    await state.update_data(channel=message.text.strip())
    await state.set_state(Verify.date_min)
    await message.answer("Дата с:")


@router.message(Verify.date_min)
async def verify_dmin(message: Message, state: FSMContext):
    await state.update_data(date_min=message.text.strip())
    await state.set_state(Verify.date_max)
    await message.answer("Дата по:")


@router.message(Verify.date_max)
async def verify_run(message: Message, state: FSMContext, pool: WorkerPool, manager: JobManager, settings: Settings):
    data = await state.get_data()
    await state.clear()

    session_string = scraping.worker_session_string(pool)
    if session_string is None:
        await message.answer("Нет воркеров.")
        return

    output = str(Path(data["input"]).with_suffix("")) + "_missed.parquet"

    try:
        params = VerifyParams(
            input=data["input"],
            channel=data["channel"],
            date_min=parse_date(data["date_min"]),
            date_max=parse_date(message.text.strip(), end_of_day=True),
            output=output,
            comment_sample=0,
        )
    except Exception as err:
        await message.answer(f"Неверные параметры: {err}")
        return

    if not manager.acquire("Верификация", cancelable=False, timeout=0):
        await message.answer(f"⛔ Занят: {manager.label}. Дождитесь завершения.")
        return

    await message.answer("Верификация запущена…")

    try:
        await scraping.do_verify(build_credentials(settings, session_string), params)
    except SystemExit as err:
        await message.answer(f"Остановлено: {err}")
        return
    except Exception as err:
        await message.answer(f"Ошибка: {err}")
        return
    finally:
        manager.release()

    if os.path.exists(output):
        await message.answer("Готово ✅")
        await _send_files(message.bot, message.chat.id, [output])
    else:
        await message.answer("Готово ✅ (пропущенных постов не найдено).")


# =============================== analysis ===============================

# tool -> (fields [(prompt, key)...], action(data) -> (text, file_path_or_None))
ANALYSIS = {
    "combine": (
        [("input (файл/папка/glob *.parquet)", "input"), ("output", "output")],
        lambda d: (analysis.combine(d["input"], d["output"], ["Group", "Message ID"]), d["output"]),
    ),
    "comments": (
        [("input posts", "input"), ("output", "output")],
        lambda d: (analysis.explode_comments(d["input"], d["output"], "parquet"), d["output"]),
    ),
    "participants": (
        [("input posts", "input"), ("output", "output")],
        lambda d: (analysis.participants(d["input"], d["output"], None, "parquet"), d["output"]),
    ),
    "summary": (
        [("input", "input"), ("output base (префикс)", "output")],
        lambda d: (analysis.summary(d["input"], d["output"], "Date", "Group", "Comments"), None),
    ),
    "sample": (
        [("input", "input"), ("output", "output")],
        lambda d: (analysis.sample(d["input"], d["output"], "Content", "Group", 10000, 20), d["output"]),
    ),
    "filter": (
        [("input", "input"), ("output base", "output"), ("ключевые слова (через запятую)", "keywords")],
        lambda d: (
            analysis.filter_keywords(
                d["input"], d["output"], "Content",
                [k.strip() for k in d["keywords"].split(",") if k.strip()], 1_000_000,
            ),
            None,
        ),
    ),
    "links": (
        [("input", "input"), ("output", "output")],
        lambda d: (analysis.links(d["input"], d["output"]), d["output"]),
    ),
    "read": (
        [("input", "input")],
        None,  # special-cased
    ),
}


@router.callback_query(FunctionCB.filter(F.key == "analysis"))
async def analysis_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    builder = InlineKeyboardBuilder()
    for tool in ANALYSIS:
        builder.button(text=tool, callback_data=ChoiceCB(scope="an_tool", value=tool))
    builder.adjust(2)
    await callback.message.answer("Инструмент анализа:", reply_markup=builder.as_markup())


@router.callback_query(ChoiceCB.filter(F.scope == "an_tool"))
async def analysis_tool(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext):
    tool = callback_data.value
    fields = ANALYSIS[tool][0]
    await state.update_data(tool=tool, idx=0, collected={})
    await state.set_state(Analysis.args)
    await callback.message.answer(f"{fields[0][0]}:")
    await callback.answer()


@router.message(Analysis.args)
async def analysis_arg(message: Message, state: FSMContext, manager: JobManager):
    data = await state.get_data()
    tool = data["tool"]
    fields, action = ANALYSIS[tool]
    idx = data["idx"]
    collected = data["collected"]

    collected[fields[idx][1]] = message.text.strip()
    idx += 1

    if idx < len(fields):
        await state.update_data(idx=idx, collected=collected)
        await message.answer(f"{fields[idx][0]}:")
        return

    await state.clear()

    if not manager.acquire("Анализ", cancelable=False, timeout=0):
        await message.answer(f"⛔ Занят: {manager.label}. Дождитесь завершения.")
        return

    await message.answer("Выполняется…")

    try:
        if tool == "read":
            df = await asyncio.to_thread(read_table, collected["input"])
            await message.answer(read_preview(df), parse_mode="HTML")
            return

        _result, file_path = await asyncio.to_thread(action, collected)
    except Exception as err:
        await message.answer(f"Ошибка: {err}")
        return
    finally:
        manager.release()

    if file_path and os.path.exists(file_path):
        await message.answer("Готово ✅")
        await _send_files(message.bot, message.chat.id, [file_path])
    else:
        await message.answer("Готово ✅")
