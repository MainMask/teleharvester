from aiogram import Router
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery, Message

from bot.keyboards.menu import main_menu
from bot.routers._common import STALE_BUTTON

router = Router()

# The last router: a button no handler took — an old message's, a step left since, or a menu path
# that is gone — and a message no step took. Answered, or nothing would happen.


@router.callback_query()
async def stale_button(callback: CallbackQuery):
    await callback.answer(STALE_BUTTON)


@router.message(StateFilter(None))  # not in a step (in one: button_step below)
async def no_step(message: Message):
    # the keyboard again too: it may have been hidden
    await message.answer("Выберите раздел в меню ниже 👇", reply_markup=main_menu())


@router.message()  # in a step no handler took the message of: one that waits for a button
async def button_step(message: Message):
    await message.answer("Нажмите кнопку выше — или /cancel, чтобы выйти.")
