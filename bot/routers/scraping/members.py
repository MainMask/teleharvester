"""A group's members as a project's base (bot/routers/projects.py)."""
import html
from pathlib import Path

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.keyboards.common import cancel_kb, scrape_kb
from bot.routers._common import menu_answer, require_text
from bot.routers.scraping import common
from bot.services import scraping
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from bot.states import Members
from modules import projects, scraped_files
from modules.scraper_creds import build_credentials
from scraper.members import MembersParams
from scraper.scrape import (
    bad_channels, bad_channels_text, parse_channels,
)

router = Router()


async def members_project(message: Message, state: FSMContext, project, deps: dict):
    """The members from a project's card (bot/routers/projects.py): the base is named after the
    project, so it is one of the project's bases (scraped_files.project_files)."""
    await state.clear()
    await state.update_data(only_project=project.name)
    await common.choose_account(message, state, "members", **deps)


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
    raw = await require_text(message)
    if raw is None:
        return
    chats = parse_channels(raw)
    if not chats:
        await message.answer("Не вижу ни одной группы. Пришлите ещё раз:")
        return
    if bad := bad_channels(chats):
        await message.answer(f"{bad_channels_text(bad)} Пришлите список групп ещё раз:")
        return
    await state.update_data(chats=raw)
    # the groups get named on the project too: its messages show them by name
    if await common.check_chats(message, state, pool, personal, chats, (await state.get_data()).get("only_project")):
        await _members_ready(message, state, dict(pool=pool, personal=personal, scrapes=scrapes))


async def _members_ready(message: Message, state: FSMContext, deps: dict):
    """The groups are checked: the project's base, named after it, is collected."""
    await _members_go(message, state, (await state.get_data())["only_project"], **deps)


async def _members_go(message: Message, state: FSMContext, name: str, pool: WorkerPool, personal,
                      scrapes: JobManager):
    data = await state.get_data()
    await state.clear()

    account = common.run_account(pool, personal, data.get("account", ""))
    if account is None:
        await menu_answer(message, "Аккаунт не найден, начните заново.")
        return

    progress = await common.take_slot(scrapes, pool, account, "Участники чата",
                                lambda text: menu_answer(message, text), message.answer)
    if progress is None:
        return
    before = scraped_files.row_count(scraped_files.members_base(name))  # the base collected before: added to
    params = MembersParams(chats=parse_channels(data["chats"]), name=name,
                           out_dir=Path(common.DEFAULT_OUT), on_progress=progress.update)
    stop = params.stop = scrapes.soft_stop()  # the job's own ⏹: a later job's can't relabel this one
    status = None
    try:
        status = await message.answer("Собираю участников…", reply_markup=scrape_kb(scrapes.key))
        path, problems = await scraping.do_members(build_credentials(account.client), params)
    except (Exception, SystemExit) as err:  # connect() raises SystemExit on a logged-out session
        await menu_answer(message, f"Ошибка: {err}")
        return
    finally:
        await common.free_slot(scrapes, pool, status)

    if problems:
        why = {chat: str(problem) for chat, problem in problems}
        named = projects.chat_list(projects.find(data["only_project"]), list(why), html=True, problems=why)
        await message.answer(f"Не всё удалось собрать:\n\n{named}", parse_mode="HTML")
    if path is None:
        await menu_answer(message, "Ни одного участника не собрано.")
        return
    await common.send_files(message.bot, message.chat.id, [str(path)])
    total = scraped_files.row_count(path)
    where = (f"в проекте «{data['only_project']}» (новых: {total - before}, всего: {total}): 📇 В контакты и "
             "📨 ЛС по базе — в его карточке")
    await menu_answer(message, f"⏹ Остановлено. Собранные участники сохранены — база {where}."
                if stop.is_set() and not common.shutting_down else
                f"Готово ✅ База — {where}.")


common.FLOW_NEXT["members"] = _ask_groups
common.CHATS_NEXT[Members.chats.state] = _members_ready
