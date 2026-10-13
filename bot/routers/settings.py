from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import MenuAction, MenuCB
from bot.keyboards.menu import SETTINGS_BUTTON, main_menu, screen_kb
from bot.services.registry import SETTINGS, screen_text
from bot.states import SetDelay, SetProfilePause
from modules.settings import Settings, parse_seconds, seconds_text

router = Router()

# ⚙️ Настройки: what the bot writes back to config.toml (the CLI reads it too). The auto-reply's
# own screen is bot/routers/autoreply.py.


def _settings_screen(settings: Settings) -> str:
    autoreply = "вкл" if settings.autoreply_enabled and settings.autoreply_text else "выкл"
    return (f"⚙️ <b>Настройки</b> — задержка <b>{seconds_text(settings.delay)}</b> · пауза профиля "
            f"<b>{seconds_text(settings.profile_pause)}</b> · автоответ <b>{autoreply}</b>\n\n{screen_text(SETTINGS)}")


@router.message(F.text == SETTINGS_BUTTON)
async def settings_screen(message: Message, state: FSMContext, settings: Settings):
    await state.clear()
    await message.answer(_settings_screen(settings), parse_mode="HTML", reply_markup=screen_kb(SETTINGS))


@router.callback_query(MenuCB.filter(F.action == MenuAction.SETTINGS))
async def back_to_settings(callback: CallbackQuery, state: FSMContext, settings: Settings):
    await state.clear()
    # navigation edits the screen in place, as the workers screen does
    await callback.message.edit_text(_settings_screen(settings), parse_mode="HTML", reply_markup=screen_kb(SETTINGS))
    await callback.answer()


# --- delay between actions ---------------------------------------------------

@router.callback_query(MenuCB.filter(F.action == MenuAction.DELAY))
async def delay_start(callback: CallbackQuery, state: FSMContext, settings: Settings):
    await callback.answer()
    await state.set_state(SetDelay.input)
    await callback.message.answer(
        f"⏱ Сейчас между действиями воркера: <b>{seconds_text(settings.delay)}</b> "
        "(рассылки, инвайтинг, контакты, вступления, реакции, опросы, жалобы).\n\n"
        "Пришлите новую задержку в секундах: <code>5-10</code> (случайная в диапазоне) "
        "или <code>7</code>.",
        parse_mode="HTML",
    )


@router.message(SetDelay.input)
async def delay_apply(message: Message, state: FSMContext, settings: Settings):
    delay = parse_seconds(message.text)
    if delay is None:
        await message.answer("Нужно число или диапазон, например 5-10 или 7. Пришлите ещё раз.")
        return

    try:
        settings.set_delay(delay)
    except (OSError, ValueError) as err:  # ValueError: a config.toml broken by hand meanwhile
        await message.answer(f"⚠️ Не удалось сохранить config.toml: {err}")
        return
    await state.clear()
    await message.answer(f"✅ Задержка: {seconds_text(delay)}. Действует сразу и в CLI.", reply_markup=main_menu())


# --- pause between accounts for profile changes ------------------------------

@router.callback_query(MenuCB.filter(F.action == MenuAction.PROFILE_PAUSE))
async def profilepause_start(callback: CallbackQuery, state: FSMContext, settings: Settings):
    await callback.answer()
    await state.set_state(SetProfilePause.input)
    await callback.message.answer(
        f"⏳ Сейчас пауза между аккаунтами при смене профиля: <b>{seconds_text(settings.profile_pause)}</b> "
        "(имя, bio, username, 2FA, фото, канал, last seen).\n\n"
        "Пришлите новую паузу в секундах: <code>60-180</code> (случайная в диапазоне) "
        "или <code>120</code>.",
        parse_mode="HTML",
    )


@router.message(SetProfilePause.input)
async def profilepause_apply(message: Message, state: FSMContext, settings: Settings):
    pause = parse_seconds(message.text)
    if pause is None:
        await message.answer("Нужно число или диапазон, например 60-180 или 120. Пришлите ещё раз.")
        return

    try:
        settings.set_profile_pause(pause)
    except (OSError, ValueError) as err:  # ValueError: a config.toml broken by hand meanwhile
        await message.answer(f"⚠️ Не удалось сохранить config.toml: {err}")
        return
    await state.clear()
    await message.answer(
        f"✅ Пауза профиля: {seconds_text(pause)}. Действует сразу и в CLI.", reply_markup=main_menu()
    )
