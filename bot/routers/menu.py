import html

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import CategoryCB, ChoiceCB
from bot.keyboards.menu import functions_kb, main_menu
from bot.services import login as sign_in
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from bot.services.registry import SECTIONS, WORKER_GROUPS, section_text

router = Router()

WELCOME = (
    "👋 Привет, <b>{name}</b>!\n\n"
    "Добро пожаловать в <b>teleharvester</b> — твою панель управления Telegram-аккаунтами.\n\n"
    "🤖 Воркеров онлайн: <b>{workers}</b>\n\n"
    "📣 <b>Рассылки</b> — в личку, в комментарии и в чаты\n"
    "💬 <b>Активность</b> — вступления, реакции, опросы, репорты\n"
    "🎯 <b>Аудитория</b> — скрапинг, инвайтинг, контакты\n"
    "🤖 <b>Воркеры</b> — аккаунты, профиль, безопасность, проверка\n\n"
    "<i>Выбери раздел в меню ниже 👇</i>"
)


@router.message(CommandStart())
async def start(message: Message, state: FSMContext, pool: WorkerPool):
    await state.clear()
    await message.answer(
        WELCOME.format(name=html.escape(message.from_user.first_name), workers=pool.count()),
        parse_mode="HTML",
        reply_markup=main_menu(),
    )


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext, manager: JobManager):
    if await state.get_state() is not None:
        # backing out of a form must not stop a job that may belong to another admin
        await sign_in.close(message.chat.id)  # a sign-in by phone waiting for its code: disconnect it now
        await state.clear()
        note = f"\nЗадача «{manager.label}» продолжает работу — /cancel ещё раз, чтобы остановить." \
            if manager.active else ""
        await message.answer("Отменено." + note, reply_markup=main_menu())
        return

    stopped = await manager.stop()  # abort the active job if one is open (a task or an interactive flow)
    if not stopped and manager.active:  # scrape/verify/analysis or a report already replaying
        await message.answer(
            f"Задача «{manager.label}» не прерывается, дождитесь завершения.", reply_markup=main_menu()
        )
        return
    await message.answer("Отменено.", reply_markup=main_menu())


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


@router.message(F.text.in_(SECTIONS))
async def show_section(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(section_text(message.text), parse_mode="HTML", reply_markup=functions_kb(message.text))


@router.callback_query(CategoryCB.filter())
async def show_worker_group(callback: CallbackQuery, callback_data: CategoryCB, state: FSMContext):
    await state.clear()

    if 0 <= callback_data.index < len(WORKER_GROUPS):
        name = WORKER_GROUPS[callback_data.index]
        # navigation edits the workers screen in place instead of piling up messages
        await callback.message.edit_text(section_text(name), parse_mode="HTML",
                                         reply_markup=functions_kb(name, back=True))

    await callback.answer()
