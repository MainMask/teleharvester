import html

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import ChoiceCB, NavCB
from bot.keyboards.common import scrape_kb, stop_kb
from bot.keyboards.menu import main_menu
from bot.routers._common import STALE_BUTTON, leave_form
from bot.services import screens
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from bot.services.registry import MAIN_MENU, SCREENS, TASKS

router = Router()


def welcome(name: str, workers: int) -> str:
    """The /start message: each main-menu button with what it holds (registry SCREENS hints)."""
    lines = []
    for label in MAIN_MENU:
        icon, title = label.split(" ", 1)
        lines.append(f"{icon} <b>{title}</b> — {SCREENS[label].hint}")
    sections = "\n".join(lines)
    return (f"👋 Здравствуйте, <b>{html.escape(name)}</b>!\n\n"
            "Добро пожаловать в <b>teleharvester</b> — вашу панель управления Telegram-аккаунтами.\n\n"
            f"🤖 Воркеров онлайн: <b>{workers}</b>\n\n{sections}\n\n"
            "<i>Выберите раздел в меню ниже 👇</i>")


@router.message(CommandStart())
async def start(message: Message, state: FSMContext, pool: WorkerPool):
    await leave_form(message, state)
    await message.answer(
        welcome(message.from_user.first_name, pool.count()),
        parse_mode="HTML",
        reply_markup=main_menu(),
    )


async def _leave_form(message: Message, state: FSMContext, manager: JobManager):
    # backing out of a form must not stop a job that may belong to another admin
    await leave_form(message, state)
    note = f"\nЗадача «{manager.label}» продолжает работу — /cancel ещё раз, чтобы остановить." \
        if manager.active else ""
    await message.answer("Отменено." + note, reply_markup=main_menu())


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext, manager: JobManager):
    if await state.get_state() is not None:
        await _leave_form(message, state, manager)
        return

    stopped = await manager.stop()  # abort the active job if one is open (a task or an interactive flow)
    if not stopped and manager.active:  # a report already replaying, a tdata import, the accounts list
        await message.answer(
            f"Задача «{manager.label}» не прерывается, дождитесь завершения.", reply_markup=main_menu()
        )
        return
    await message.answer("Отменено.", reply_markup=main_menu())


@screens.view(TASKS)  # 📊 Задачи: the same
async def tasks(message: Message, state: FSMContext, edit: bool, manager: JobManager, scrapes: JobManager,
                analyses: JobManager, **_):
    await status(message, manager, scrapes, analyses)


@router.message(Command("status"))
async def status(message: Message, manager: JobManager, scrapes: JobManager, analyses: JobManager):
    """What runs now, each job with its 📊 and ⏹ as under its start message (which may be far up);
    a form under way stays where it is."""
    active = [(slot, markup) for slot, markup in ((manager, stop_kb()), (scrapes, scrape_kb()), (analyses, None))
              if slot.active]
    if not active:
        await message.answer("Сейчас ничего не выполняется.")
    for slot, markup in active:
        if slot.progress is None:  # a dialog holding the slot, or a job with nothing to count
            await message.answer(f"▶️ {slot.label}")
        else:
            await message.answer(slot.progress.render(slot.label), reply_markup=markup)


@router.callback_query(ChoiceCB.filter(F.scope == "cancel"))
async def cancel_button(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext, manager: JobManager):
    """✖️ Отмена under a prompt (keyboards.common.cancel_kb): /cancel's form branch, only at its own step."""
    if await state.get_state() != callback_data.value.replace(".", ":"):
        await callback.answer(STALE_BUTTON)
        return
    await callback.answer()
    await _leave_form(callback.message, state, manager)


@router.callback_query(ChoiceCB.filter(F.scope == "job_stop"))
async def stop_job(callback: CallbackQuery, manager: JobManager):
    stopped = await manager.stop()
    await callback.answer("Останавливаю…" if stopped else "Нечего останавливать (или задача не прерывается).")


@router.callback_query(ChoiceCB.filter(F.scope == "job_progress"))
async def show_progress(callback: CallbackQuery, callback_data: ChoiceCB, manager: JobManager,
                        scrapes: JobManager):
    slot = scrapes if callback_data.value == "scrape" else manager  # the scraper has its own slot
    if slot.progress is None:
        await callback.answer("Нет активной задачи.")
        return
    await callback.answer(slot.progress.render(slot.label), show_alert=True)


# Every screen, opened the same way (bot/services/screens.py): a main-menu button — from any form's
# step too, this router being the first (bot/app.py) — as a new message; a screen's button
# (›, ⬅️ Назад) in that message's place instead of piling up messages.

async def _open(title: str, message: Message, state: FSMContext, edit: bool, deps: dict):
    if not SCREENS[title].keeps_form:
        await leave_form(message, state)
    await screens.open_screen(title, message, state, edit, deps)


@router.message(F.text.in_(MAIN_MENU))
async def open_from_menu(message: Message, state: FSMContext, **deps):
    await _open(message.text, message, state, False, deps)


@router.callback_query(NavCB.filter(F.screen.in_(SCREENS)))
async def open_by_button(callback: CallbackQuery, callback_data: NavCB, state: FSMContext, **deps):
    await _open(callback_data.screen, callback.message, state, True, deps)
    await callback.answer()
