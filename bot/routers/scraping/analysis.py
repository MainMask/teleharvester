"""The analysis tools (modules/analysis_tools.py) on a project's files, from its card: in a child
process, in a slot of their own (`analyses`), on no account."""
import os
import secrets
from pathlib import Path

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import ChoiceCB
from bot.keyboards.common import cancel_kb, choice_kb
from bot.routers._common import STALE_BUTTON, menu_answer, require_text
from bot.routers.scraping import common
from bot.services import scraping
from bot.services.jobs import JobManager
from bot.states import Analysis
from modules import analysis_tools, scraped_files

router = Router()


def _tools_kb(tools, more: bool, token: str):
    """`token`: the project's menu (analysis_project) — its buttons act on that project only."""
    suffix = f".{token}"
    options = [(analysis_tools.TOOLS[tool][0], tool + suffix) for tool in tools]
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
        reply_markup=_tools_kb(analysis_tools.MAIN_TOOLS, more=True, token=token),
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
        await callback.message.answer("Ещё инструменты:",
                                      reply_markup=_tools_kb(analysis_tools.MORE_TOOLS, more=False, token=token))
        return
    if tool not in analysis_tools.TOOLS:
        return

    label, hint, kind, _ = analysis_tools.TOOLS[tool]
    project = data["only_project"]
    if kind == "any":
        files = scraped_files.project_files(project)
    elif kind == "combine":  # the scrapes' files: the previous combine's is not merged into the next
        files = scraped_files.scrape_posts(project)
    else:
        files = scraped_files.project_files(project, kinds=("posts",))
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
    data = await state.get_data()
    tool = data["tool"]
    if tool == "filter":
        await state.update_data(path=path)
        await state.set_state(Analysis.keywords)
        await message.answer("Слова через запятую (например: крипта, биткоин):",
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


async def _run_analysis(message: Message, analyses: JobManager, tool: str, path, data: dict):
    # summary/filter write several files by a prefix and return no path; snapshot the
    # output dir (the input's) so the created files can be sent back, the way scrape
    # does (before acquire, as there).
    multi_file = tool in analysis_tools.MULTI_FILE
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

    if multi_file:  # named after their output base
        output = scraped_files.output_path(path, analysis_tools.MULTI_FILE[tool])
        files = scraping.new_files(out_dir, before, (Path(output).name,))
        if files:
            await common.send_files(message.bot, message.chat.id, files)
        await menu_answer(message, "Готово ✅" if files else "Готово, но ничего не найдено.")
        return

    if file_path and os.path.exists(file_path):
        await message.answer("Готово ✅")
        await common.send_files(message.bot, message.chat.id, [file_path])
        await menu_answer(message, "Анализ завершён. Вы в главном меню.")
    else:
        await menu_answer(message, "Готово ✅")
