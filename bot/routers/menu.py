from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import CategoryCB, ChoiceCB, MenuAction, MenuCB
from bot.keyboards.menu import categories_kb, functions_kb, main_menu
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from bot.services.registry import categories

router = Router()

WELCOME = (
    "🛡 <b>Панель управления teleharvester</b>\n\n"
    "Этот бот — <b>хост</b>: он только отдаёт команды и сам не выполняет рискованных "
    "действий. Вся опасная работа делегируется <b>воркер-аккаунтам</b> из <code>sessions/</code>.\n\n"
    "Воркеров подключено: <b>{workers}</b>."
)


@router.message(CommandStart())
async def start(message: Message, state: FSMContext, pool: WorkerPool):
    await state.clear()
    await message.answer(
        WELCOME.format(workers=pool.count()),
        parse_mode="HTML",
        reply_markup=main_menu(),
    )


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext, manager: JobManager):
    if await state.get_state() is not None:
        # backing out of a form must not stop a job that may belong to another admin
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


@router.message(F.text == "📋 Функции")
async def show_categories(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Категории:", reply_markup=categories_kb())


@router.callback_query(MenuCB.filter(F.action == MenuAction.CATEGORIES))
async def back_to_categories(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("Категории:", reply_markup=categories_kb())
    await callback.answer()


@router.callback_query(CategoryCB.filter())
async def show_functions(callback: CallbackQuery, callback_data: CategoryCB, state: FSMContext):
    await state.clear()
    names = categories()

    if 0 <= callback_data.index < len(names):
        name = names[callback_data.index]
        await callback.message.answer(name, reply_markup=functions_kb(name))

    await callback.answer()
