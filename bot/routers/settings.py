from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import MenuAction, MenuCB
from bot.keyboards.common import cancel_kb
from bot.keyboards.menu import screen_kb
from bot.services import screens
from bot.services.registry import SETTINGS
from bot.states import SetDelay, SetProfilePause
from modules.settings import Settings, parse_seconds, seconds_text

router = Router()

# ⚙️ Настройки: the delays the bot writes back to config.toml (the CLI reads them too), and the
# auto-reply kept there too: its own screen, bot/routers/autoreply.py.


def _header(settings: Settings) -> str:
    autoreply = "включён" if settings.autoreply_enabled and settings.autoreply_text else "выключен"
    return (f"⚙️ <b>Настройки</b> — задержка <b>{seconds_text(settings.delay)}</b> · "
            f"пауза профиля <b>{seconds_text(settings.profile_pause)}</b> · автоответ <b>{autoreply}</b>")


def _settings_screen(settings: Settings) -> str:
    return screens.screen_text(SETTINGS, _header(settings))


@screens.view(SETTINGS)  # by ⬅️ from the auto-reply's screen too: the settings come back in its place
async def settings_screen(message: Message, state: FSMContext, edit: bool, settings: Settings, **_):
    await screens.show(message, SETTINGS, edit, _header(settings))


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
        parse_mode="HTML", reply_markup=cancel_kb(SetDelay.input),
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
    # the settings screen again, with the new value: the one above still shows the old
    await message.answer(
        f"✅ Задержка: {seconds_text(delay)}. Действует сразу и в CLI.\n\n{_settings_screen(settings)}",
        parse_mode="HTML", reply_markup=screen_kb(SETTINGS),
    )


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
        parse_mode="HTML", reply_markup=cancel_kb(SetProfilePause.input),
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
        f"✅ Пауза профиля: {seconds_text(pause)}. Действует сразу и в CLI.\n\n{_settings_screen(settings)}",
        parse_mode="HTML", reply_markup=screen_kb(SETTINGS),
    )
