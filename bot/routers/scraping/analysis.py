"""The analysis tools (modules/analysis_tools.py) on a project's files, from its card: in a child
process, in a slot of their own (`analyses`), on no account."""
import html
import os
import secrets
from pathlib import Path

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import ChoiceCB
from bot.keyboards.common import cancel_kb, choice_kb
from bot.keyboards.menu import nav_button
from bot.routers._common import pick_from, require_text, stale_tap
from bot.routers.scraping import common
from bot.services import scraping
from bot.services.screens import title_line
from bot.services.jobs import JobManager, busy
from bot.states import Analysis
from modules import analysis_tools, scraped_files
from modules.registry import COLLECT, title

router = Router()


def _tools_kb(token: str, project: int):
    """Every tool, one a row (`token`: the project's menu, analysis_project — its buttons act on that project
    only), then ⬅️ Назад to `project`'s 📥 Сбор, in place."""
    markup = choice_kb("an_tool", [(label, f"{tool}.{token}") for tool, (label, *_) in analysis_tools.TOOLS.items()])
    markup.inline_keyboard.append([nav_button("⬅️ Назад", COLLECT, project)])
    return markup


def _analysis_text(project: str) -> str:
    """The analysis menu's message (HTML): its path from the project's card, then each tool with what it does, as
    a screen's."""
    lines = [f"<b>{html.escape(label)}</b> — {html.escape(hint)}" for label, hint, *_ in analysis_tools.TOOLS.values()]
    return (f"{title_line(COLLECT, project)} › <b>{html.escape(title('analysis'))}</b>\n\n"
            "Утилиты для файлов скрапа. Аккаунты не нужны: бот читает файл и присылает результат.\n\n"
            + "\n".join(lines))


async def analysis_project(message: Message, state: FSMContext, project):
    """The analysis tools, from a project's 📥 Сбор (bot/routers/projects/card.py): they offer its files only."""
    await state.clear()
    # its buttons carry the token, as a card's: an older menu's must not act on this one
    token = secrets.token_hex(4)
    await state.update_data(only_project=project.name, analysis_project=project.id, analysis_token=token)
    await message.edit_text(_analysis_text(project.name), parse_mode="HTML",  # in 📥 Сбор's place
                            reply_markup=_tools_kb(token, project.id))


@router.callback_query(ChoiceCB.filter(F.scope == "an_tool"))
async def analysis_tool(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext):
    tool, _, token = callback_data.value.partition(".")
    data = await state.get_data()
    # a project's menu shown before the last one, or no tool of it
    if token != data.get("analysis_token") or tool not in analysis_tools.TOOLS:
        await stale_tap(callback)
        return
    await callback.answer()

    label, hint, kind, _ = analysis_tools.TOOLS[tool]
    files = analysis_tools.files(tool, data["only_project"])
    options = [(f"📄 {name}", str(i)) for i, (_, name) in enumerate(files)]
    if kind == "combine":
        options = [(f"🗂 Все файлы постов ({len(files)})", "all")] if len(files) > 1 else []
        ask = (f"{'Нажмите кнопку или п' if options else 'П'}ришлите путь к папке или шаблон "
               f"(например, {scraped_files.BASES_DIR}/*_posts*.parquet):")
    elif files:
        ask = "Выберите файл — или пришлите путь к нему на сервере:"
    elif kind == "any":
        ask = "У проекта нет файлов. Пришлите путь к файлу на сервере:"
    else:
        ask = "У проекта нет файлов с постами. Пришлите путь к файлу на сервере:"

    await state.set_state(Analysis.file)
    await state.update_data(tool=tool, files=[path for path, _ in files])
    await callback.message.answer(
        f"<b>{html.escape(label)}</b>\n{html.escape(hint)}\n\n{html.escape(ask)}", parse_mode="HTML",
        reply_markup=cancel_kb(Analysis.file, choice_kb("an_file", options) if options else None),
    )


@router.callback_query(Analysis.file, ChoiceCB.filter(F.scope == "an_file"))
async def analysis_file_pick(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext,
                             analyses: JobManager):
    data = await state.get_data()
    files = data.get("files", [])
    if callback_data.value == "all" and data.get("tool") == "combine":  # 🧩's own: another tool takes one file
        path = files
    elif (path := pick_from(files, callback_data.value)) is None:
        await stale_tap(callback)
        return
    await callback.answer()
    await _analysis_file_chosen(callback.message, state, analyses, path)


@router.message(Analysis.file)
async def analysis_file_text(message: Message, state: FSMContext, analyses: JobManager):
    path = await require_text(message)
    if path is None:
        return
    await _analysis_file_chosen(message, state, analyses, path)


async def _analysis_file_chosen(message: Message, state: FSMContext, analyses: JobManager, path):
    data = await state.get_data()
    tool = data["tool"]
    if tool == "filter":
        await state.update_data(path=path)
        await state.set_state(Analysis.keywords)
        await message.answer(f"{analysis_tools.TOOLS[tool][0]} — {analysis_tools.KEYWORDS_ASK}:",
                             reply_markup=cancel_kb(Analysis.keywords))
        return
    await _clear_keeping_menu(state)
    # the project: combine writes the project's own file
    await _run_analysis(message, analyses, tool, path, {"project": data["only_project"]})


@router.message(Analysis.keywords)
async def analysis_keywords(message: Message, state: FSMContext, analyses: JobManager):
    raw = await require_text(message)
    if raw is None:
        return
    keywords = analysis_tools.keywords(raw)
    if not keywords:
        await message.answer("Нужно хотя бы одно слово. Пришлите ещё раз:")
        return
    data = await state.get_data()
    await _clear_keeping_menu(state)
    await _run_analysis(message, analyses, data["tool"], data["path"], {"keywords": keywords})


async def _clear_keeping_menu(state: FSMContext):
    """The flow's step is over; a project's analysis menu (its token) keeps working on its project."""
    data = await state.get_data()
    await state.clear()
    if data.get("analysis_token"):
        await state.update_data(only_project=data["only_project"], analysis_project=data.get("analysis_project", 0),
                                analysis_token=data["analysis_token"])


async def _run_analysis(message: Message, analyses: JobManager, tool: str, path, data: dict):
    # summary/filter write several files by a prefix and return no path; snapshot the
    # output dir (the input's) so the created files can be sent back, the way scrape
    # does (before acquire, as there).
    multi_file = tool in analysis_tools.MULTI_FILE
    out_dir = str(Path(path).parent) if multi_file else None
    try:
        before = scraping.dir_snapshot(out_dir) if multi_file else None
    except OSError as err:
        await common.to_collect(message, f"Папка вывода недоступна: {err}", analyses)
        return

    if not analyses.acquire(title("analysis"), cancelable=False, timeout=0):
        await common.to_collect(message, busy(analyses.label), analyses)
        return

    label = analysis_tools.TOOLS[tool][0]
    try:
        await message.answer(f"{label} — выполняется…")
        text, file_path = await scraping.do_analysis(tool, path, data)
        if tool == "read":
            await message.answer(text, parse_mode="HTML")
            await common.to_collect(message, f"✅ {label}: готово.", analyses)
            return
    except (Exception, SystemExit) as err:  # scraper.analysis raises SystemExit on bad input
        await common.to_collect(message, f"⚠️ Ошибка: {err}", analyses)
        return
    finally:
        analyses.release()

    if isinstance(text, str) and text:  # summary returns None; links a top list
        await message.answer(text)

    if multi_file:  # named after their output base
        output = scraped_files.output_path(path, analysis_tools.MULTI_FILE[tool])
        files = scraping.new_files(out_dir, before, (Path(output).name,))
        if files:
            await common.send_files(message.bot, message.chat.id, files)
        await common.to_collect(message, f"✅ {label}: готово." if files else f"✅ {label}: ничего не найдено.",
                                analyses)
        return

    if file_path and os.path.exists(file_path):
        await common.send_files(message.bot, message.chat.id, [file_path])
    await common.to_collect(message, f"✅ {label}: готово.", analyses)
