from collections.abc import Callable
from dataclasses import dataclass

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import ChoiceCB, MenuAction, MenuCB
from bot.keyboards.common import cancel_kb, choice_kb
from bot.keyboards.menu import screen_kb
from bot.routers._common import STALE_STEP, back_answer, left_flow
from bot.services import screens
from bot.states import SetSetting
from modules.registry import SCREENS, SETTINGS
from modules.settings import Settings, count_text, parse_seconds, seconds_text

router = Router()

# ⚙️ Настройки: the values the bot writes back to config.toml; the auto-reply is each
# project's own (🎯 База и рассылка → 📬 Автоответ, bot/routers/autoreply.py). The chat campaign's trigger and
# messages per worker are asked (and saved) by 🔔 Чат — по слову itself (bot/routers/chat_broadcasts.py).


@dataclass(frozen=True)
class _Setting:
    shown: Callable  # the value as the screen shows it (HTML)
    current: Callable[[Settings], object]
    ask: str  # what to send (HTML)
    parse: Callable[[str], object]  # None: asked again
    retry: str
    save: Callable[[Settings, object], None]
    unlimited: bool = False  # 0 means no limit: a button says so


# the per-account daily caps, [limits] in config.toml
DAILY_CAPS = {MenuAction.CAP_PM: "per_account_daily", MenuAction.CAP_INVITE: "invite_per_account_daily",
              MenuAction.CAP_CONTACTS: "contacts_per_account_daily"}


UNLIMITED = "♾ Без лимита"


def _count(raw: str) -> int | None:
    return int(raw) if raw.strip().isdecimal() else None


VALUES = {
    MenuAction.DELAY: _Setting(
        seconds_text, lambda settings: settings.delay,
        "Пришлите новую задержку в секундах: <code>5-10</code> (случайная в диапазоне) или <code>7</code>:",
        parse_seconds, "Нужно число или диапазон, например 5-10 или 7. Пришлите ещё раз:",
        lambda settings, value: settings.set_delay(value)),
    MenuAction.PROFILE_PAUSE: _Setting(
        seconds_text, lambda settings: settings.profile_pause,
        "Пришлите новую паузу в секундах: <code>60-180</code> (случайная в диапазоне) или <code>120</code>:",
        parse_seconds, "Нужно число или диапазон, например 60-180 или 120. Пришлите ещё раз:",
        lambda settings, value: settings.set_profile_pause(value)),
    **{action: _Setting(
        count_text, lambda settings, key=key: getattr(settings, key),
        f"Пришлите число на аккаунт в сутки или нажмите «{UNLIMITED}»:",
        _count, f"Нужно целое число или «{UNLIMITED}». Пришлите ещё раз:",
        lambda settings, value, key=key: settings.set_daily_cap(key, value), unlimited=True)
       for action, key in DAILY_CAPS.items()},
}
_BUTTONS = {button.action: button for button in SCREENS[SETTINGS].buttons}


def _on_button(setting: _Setting, value) -> str:
    """The value as its button shows it: no limit as ♾, short enough to share a row."""
    return "♾" if setting.unlimited and value == 0 else setting.shown(value)


def _titles(settings: Settings) -> dict:
    """Each button with its value on it — ⏱ Задержка: 5–10 с, 📨 ЛС: ♾ —, as 🔥 Прогрев's are (screens.show's
    `titles`)."""
    return {action.value: f"{_BUTTONS[action.value].title}: {_on_button(setting, setting.current(settings))}"
            for action, setting in VALUES.items()}


HEADER = f"{screens.title_line(SETTINGS)} — общие для всех проектов"


@screens.view(SETTINGS)
async def settings_screen(message: Message, state: FSMContext, edit: bool, settings: Settings, **_):
    await screens.show(message, SETTINGS, edit, HEADER, titles=_titles(settings))


@router.callback_query(MenuCB.filter(F.action.in_(VALUES)))
async def setting_start(callback: CallbackQuery, callback_data: MenuCB, state: FSMContext, settings: Settings):
    await callback.answer()
    setting, button = VALUES[callback_data.action], _BUTTONS[callback_data.action.value]
    await state.clear()
    await state.set_state(SetSetting.value)
    await state.update_data(setting=callback_data.action.value)
    markup = choice_kb("set_unlim", [(UNLIMITED, "0")]) if setting.unlimited else None
    await callback.message.answer(
        f"{button.title} — сейчас <b>{setting.shown(setting.current(settings))}</b> ({button.hint}).\n\n{setting.ask}",
        parse_mode="HTML", reply_markup=cancel_kb(SetSetting.value, markup),
    )


@router.message(SetSetting.value)
async def setting_typed(message: Message, state: FSMContext, settings: Settings):
    await _apply(message, state, settings, message.text or "")


@router.callback_query(ChoiceCB.filter(F.scope == "set_unlim"))
async def setting_unlimited(callback: CallbackQuery, state: FSMContext, settings: Settings):
    """♾ Без лимита under a cap's prompt: as a typed 0, only while the chat is still at its step."""
    if await left_flow(callback, state, SetSetting.value):
        return
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)  # chosen once
    await _apply(callback.message, state, settings, "0")


async def _apply(message: Message, state: FSMContext, settings: Settings, raw: str):
    action = (await state.get_data()).get("setting")
    if action not in _BUTTONS or MenuAction(action) not in VALUES:
        await state.clear()
        await back_answer(message, STALE_STEP, SETTINGS)
        return
    setting = VALUES[MenuAction(action)]
    value = setting.parse(raw)
    if value is None:
        await message.answer(setting.retry)
        return

    try:
        setting.save(settings, value)
    except (OSError, ValueError) as err:  # ValueError: a config.toml broken by hand meanwhile
        await message.answer(f"⚠️ Не удалось сохранить config.toml: {err}")
        return
    await state.clear()
    # the settings screen again, with the new value: the one above still shows the old
    title = _BUTTONS[action].title.split(" ", 1)[1]
    titles = _titles(settings)
    await message.answer(f"✅ {title}: {setting.shown(value)}. Действует сразу.\n\n"
                         f"{screens.screen_text(SETTINGS, HEADER, titles=titles)}",
                         parse_mode="HTML", reply_markup=screen_kb(SETTINGS, titles=titles))
