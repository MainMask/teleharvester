"""Workers moved by ticks: 📦 free accounts into the project (➕ Добавить), ↪️ the project's out of it."""
import html

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import ChoiceCB, MenuAction, MenuCB
from bot.keyboards.common import cancel_kb, choice_kb, marks_kb
from bot.keyboards.menu import back_kb
from bot.routers._common import back_answer, ensure_workers, marks_step
from bot.services import screens
from bot.services.delegation import WorkerPool
from bot.services.workspaces import Workspace, Workspaces, move_out, take_free
from bot.states import MoveWorkers
from modules import projects
from modules.registry import ACCOUNTS
from modules.scraper_creds import scrape_accounts

router = Router()


# The lists of the worker picker (profile.py, keyboards.marks_kb): ✅ / ⬜, ☑️ Все, ⬜ Никого, then ▶️.

TICKS = [("☑️ Все", "all"), ("⬜ Никого", "none")]
FREE_HEAD = "📦 <b>Свободные аккаунты</b> — ещё ни в одном проекте. Отмеченные станут воркерами этого проекта."
OUT_HEAD = ("↪️ <b>Перенести воркеров</b> — в другой проект или в свободные. Ограничения от @SpamBot и прокси "
            "аккаунты унесут с собой; люди, добавленные ими в контакты, в этом проекте перейдут к другим воркерам.")


def _ticks_kb(step, scope: str, labels: list[str], picked: list[int], page: int, go: str):
    return cancel_kb(step, marks_kb(scope, labels, picked, page, TICKS + [(go, "go")], (2, 1)))


def _ticks_text(head: str, picked: list[int], total: int) -> str:
    return f"{head}\n\nОтмечено: {len(picked)} из {total}"


async def _ask_ticks(message: Message, state: FSMContext, step, head: str, scope: str, paths: list[str],
                     labels: list[str], go: str, edit: bool = False):
    """The list to tick; `edit`: in place of the message (a screen's, ➕ Добавить's), else as a new one."""
    await state.clear()  # an abandoned flow's data must not reach this one
    await state.set_state(step)
    await state.update_data(move_paths=paths, move_labels=labels, picked=[], move_page=0)
    await screens.send(message, edit)(_ticks_text(head, [], len(paths)), parse_mode="HTML",
                                      reply_markup=_ticks_kb(step, scope, labels, [], 0, go))


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
                                         reply_markup=_ticks_kb(await state.get_state(), scope, labels, new_picked,
                                                                new_page, go))
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
    free = [path for path in projects.account_files(projects.FREE_DIR) if path.endswith(".jsession")]
    if not free:  # a pop-up over ➕ Добавить's menu
        await callback.answer(f"Свободных аккаунтов нет — все уже в проектах (свободные — файлы .jsession "
                              f"в {projects.FREE_DIR}/).", show_alert=True)
        return
    await callback.answer()
    await _ask_ticks(callback.message, state, MoveWorkers.free, FREE_HEAD, "mv_free", free,
                     [projects.account_label(path) for path in free], "▶️ Добавить в проект", edit=True)


@router.callback_query(MoveWorkers.free, ChoiceCB.filter(F.scope == "mv_free"))
async def free_pick(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext, pool: WorkerPool,
                    workspace: Workspace):
    paths = await _ticked(callback, callback_data, state, FREE_HEAD, "mv_free", "▶️ Добавить в проект")
    if paths is None:
        return
    await state.clear()
    moved, failed = take_free(pool, paths)
    await callback.message.edit_text(
        _moved_text("✅ Добавлено в проект", moved, failed, len(paths)) + f"\n🤖 Воркеров в проекте: {pool.count()}",
        parse_mode="HTML", reply_markup=back_kb(ACCOUNTS, workspace.project_id, fresh=False))


@router.callback_query(MenuCB.filter(F.action == MenuAction.MOVE))
async def move_start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool):
    """↪️ Перенести: the project's workers to tick; one in use now is marked, and stays."""
    if not await ensure_workers(callback, pool):
        return
    # a StringSession has nothing to load it by elsewhere: moved by hand
    workers = [account for account in scrape_accounts(pool.storage) if account.path.endswith(".jsession")]
    labels = [f"{account.label}{' ⏳ занят' if pool.busy(account.path) or pool.scraped(account.path) else ''}"
              for account in workers]
    await _ask_ticks(callback.message, state, MoveWorkers.out, OUT_HEAD, "mv_out",  # in 🤖 Аккаунты' place
                     [account.path for account in workers], labels, "▶️ Далее", edit=True)


@router.callback_query(MoveWorkers.out, ChoiceCB.filter(F.scope == "mv_out"))
async def move_pick(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext, workspace: Workspace):
    paths = await _ticked(callback, callback_data, state, OUT_HEAD, "mv_out", "▶️ Далее")
    if paths is None:
        return
    try:
        others = [project for project in projects.load() if project.id != workspace.project_id]
    except ValueError as err:
        await state.clear()
        await back_answer(callback.message, f"⚠️ {err}", ACCOUNTS, workspace.project_id)
        return
    await state.set_state(MoveWorkers.target)
    await state.update_data(move_chosen=paths)
    await callback.message.edit_text(
        f"Куда перенести отмеченных — {len(paths)}?",
        reply_markup=cancel_kb(MoveWorkers.target, choice_kb(
            "mv_to", [*((f"📁 {project.name}", str(project.id)) for project in others), ("📦 В свободные", "free")])))


@router.callback_query(MoveWorkers.target, ChoiceCB.filter(F.scope == "mv_to"))
async def move_to(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext, pool: WorkerPool,
                  workspace: Workspace, workspaces: Workspaces):
    await callback.answer()
    paths = (await state.get_data())["move_chosen"]
    await state.clear()
    try:
        project = None if callback_data.value == "free" else next(
            p for p in projects.load() if str(p.id) == callback_data.value)
    except (ValueError, StopIteration):  # an unreadable file, or the project deleted meanwhile
        await callback.message.edit_text("Этого проекта уже нет — начните заново.",
                                         reply_markup=back_kb(ACCOUNTS, workspace.project_id, fresh=False))
        return
    target = None if project is None else workspaces.get(project.id)  # else it reads its folder when opened
    moved, failed = await move_out(pool, paths, project, target)
    where = "в свободные" if project is None else f"в проект «{project.name}»"
    await callback.message.edit_text(_moved_text(f"✅ Перенесено {html.escape(where)}", moved, failed, len(paths)),
                                     parse_mode="HTML",
                                     reply_markup=back_kb(ACCOUNTS, workspace.project_id, fresh=False))
