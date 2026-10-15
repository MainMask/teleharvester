"""🤖 Аккаунты → ➕ Добавить by phone (a worker or a personal account)
(the logic: modules/login.py; the bot's own side: bot/services/login.py)."""

import asyncio

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from telethon import errors
from telethon.tl.types.auth import SentCodeTypeApp

from bot.callbacks import ChoiceCB, MenuAction, MenuCB
from bot.keyboards.common import cancel_kb, choice_kb
from bot.keyboards.menu import main_menu
from bot.routers._common import delete_quietly
from bot.services import login as logins
from bot.services.delegation import WorkerPool
from modules.registry import menu_path
from bot.states import AddByPhone
from modules import login as sign_in

router = Router()

CODE_PROMPT = (
    "Пришлите код <b>через пробелы или дефисы</b>, например <code>1 2 3 4 5</code>: код, "
    "отправленный в Telegram целиком, Telegram аннулирует."
)
RESEND_KB = choice_kb("phone_resend", [("📨 Отправить код заново", "resend")])


# =============================== add by phone ===============================

async def _end_login(message: Message, state: FSMContext, text: str):
    await logins.close(message.chat.id)
    await state.clear()
    await message.answer(text, reply_markup=main_menu())


def add_kb():
    """By phone: a worker or a personal account; from tdata, or one in no project: a worker
    (bot/routers/accounts.py)."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🤖 Воркер — по номеру", callback_data=ChoiceCB(scope="phone_role", value="worker"))
    builder.button(text="👤 Личный — по номеру", callback_data=ChoiceCB(scope="phone_role", value=sign_in.PERSONAL))
    builder.button(text="📥 Воркер — из tdata (ZIP)", callback_data=MenuCB(action=MenuAction.TDATA))
    builder.button(text="📦 Воркеры — из свободных", callback_data=MenuCB(action=MenuAction.FREE))
    builder.adjust(1)
    return builder.as_markup()


@router.callback_query(MenuCB.filter(F.action == MenuAction.ADD))
async def phone_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.answer("Кого и как добавляем в проект? Личный аккаунт — общий для всех проектов, "
                                  "только для скрапа.", reply_markup=add_kb())


@router.callback_query(ChoiceCB.filter(F.scope == "phone_role"))
async def phone_role(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext):
    """A worker joins the pool; a personal account goes to personal_sessions/ (scrapes only)."""
    await callback.answer()
    await state.clear()
    await state.update_data(role=callback_data.value)
    await state.set_state(AddByPhone.phone)
    await callback.message.answer(
        "Номер телефона аккаунта в международном формате, например <code>+79991234567</code>.\n"
        "Аккаунт должен быть уже зарегистрирован в Telegram.",
        parse_mode="HTML", reply_markup=cancel_kb(AddByPhone.phone),
    )


@router.message(AddByPhone.phone)
async def phone_input(message: Message, state: FSMContext, pool: WorkerPool, personal):
    phone = sign_in.normalize_phone(message.text)
    if phone is None:
        await message.answer("Не похоже на номер. Пришлите его в формате +79991234567:")
        return
    role = (await state.get_data()).get("role")
    if refused := sign_in.refusal(role, phone, pool.storage, personal):
        await _end_login(message, state, refused)
        return
    try:
        proxy = sign_in.role_proxy(role, pool.storage)
    except ValueError as err:
        await _end_login(message, state, f"⚠️ {err}.")
        return

    await logins.close(message.chat.id)  # a sign-in started before in this chat
    client, generator = sign_in.new_client(proxy)
    try:
        sent = await sign_in.request_code(client, phone)
    except Exception as err:
        try:
            await client.disconnect()
        except Exception:
            pass
        await _end_login(message, state, f"⚠️ Код не отправлен: {sign_in.send_error(err)}.")
        return

    logins.open_login(message.chat.id, sign_in.Login(client, phone, proxy, generator))
    await state.set_state(AddByPhone.code)
    where = "в Telegram на другом устройстве аккаунта" if isinstance(
        getattr(sent, "type", None), SentCodeTypeApp) else "по SMS или звонком"
    await message.answer(f"📨 Код отправлен {where}.\n{CODE_PROMPT}", parse_mode="HTML",
                         reply_markup=cancel_kb(AddByPhone.code, RESEND_KB))


@router.callback_query(ChoiceCB.filter(F.scope == "phone_resend"))
async def phone_resend(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    login = logins.get(callback.message.chat.id)
    # a menu button cleared the flow meanwhile: without its role a personal account would be saved as a worker
    if login is None or "role" not in await state.get_data():
        await logins.close(callback.message.chat.id)
        await callback.message.answer(f"Вход устарел — начните заново: {menu_path('add')}.")
        return
    try:  # as the first request: a dead proxy must not hang the button
        await asyncio.wait_for(login.client.send_code_request(login.phone), sign_in.FETCH_TIMEOUT * 2)
    except errors.FloodWaitError as err:
        await callback.message.answer(f"⚠️ Слишком много попыток, подождите {err.seconds} с.")
        return
    except Exception as err:  # a timeout has no text
        await callback.message.answer(f"⚠️ Код не отправлен: {str(err) or type(err).__name__}")
        return
    await state.set_state(AddByPhone.code)
    await callback.message.answer(f"📨 Новый код отправлен.\n{CODE_PROMPT}", parse_mode="HTML",
                                  reply_markup=cancel_kb(AddByPhone.code))


@router.message(AddByPhone.code)
async def code_input(message: Message, state: FSMContext, pool: WorkerPool, personal):
    login = logins.get(message.chat.id)
    if login is None:
        await _end_login(message, state, f"Вход устарел — начните заново: {menu_path('add')}.")
        return
    code = sign_in.code_digits(message.text)
    await delete_quietly(message)
    if code is None:
        await message.answer(CODE_PROMPT, parse_mode="HTML")
        return

    try:
        me = await login.client.sign_in(login.phone, code)
    except errors.SessionPasswordNeededError:
        await state.set_state(AddByPhone.password)
        await message.answer("🔐 У аккаунта включена двухэтапная проверка. Пришлите пароль (сообщение удалю):",
                             reply_markup=cancel_kb(AddByPhone.password))
        return
    except errors.PhoneCodeInvalidError:
        await message.answer(f"Неверный код. {CODE_PROMPT}", parse_mode="HTML")
        return
    except errors.PhoneCodeExpiredError:
        await message.answer("⌛ Код истёк (или Telegram аннулировал его, увидев целиком в сообщении). "
                             "Запросите новый и пришлите через пробелы.",
                             reply_markup=cancel_kb(AddByPhone.code, RESEND_KB))
        return
    except errors.PhoneNumberUnoccupiedError:
        await _end_login(message, state,
                         "⚠️ Номер не зарегистрирован в Telegram: бот добавляет только существующие аккаунты.")
        return
    except errors.FloodWaitError as err:
        await message.answer(f"⚠️ Слишком много попыток, подождите {err.seconds} с и пришлите код снова.")
        return
    except Exception as err:
        await _end_login(message, state, f"⚠️ Вход не удался: {err}")
        return
    await _finish(message, state, pool, personal, login, me)


@router.message(AddByPhone.password)
async def password_input(message: Message, state: FSMContext, pool: WorkerPool, personal):
    login = logins.get(message.chat.id)
    if login is None:
        await _end_login(message, state, f"Вход устарел — начните заново: {menu_path('add')}.")
        return
    password = message.text  # as typed: spaces may be part of it
    await delete_quietly(message)
    if not password:
        await message.answer("Ожидается пароль текстом:")
        return

    try:
        me = await login.client.sign_in(password=password)
    except errors.PasswordHashInvalidError:
        await message.answer("Неверный пароль. Пришлите ещё раз:")
        return
    except errors.FloodWaitError as err:
        await message.answer(f"⚠️ Слишком много попыток, подождите {err.seconds} с и пришлите пароль снова.")
        return
    except Exception as err:
        await _end_login(message, state, f"⚠️ Вход не удался: {err}")
        return
    login.password = password  # kept in the .jsession, as the tdata import does
    await _finish(message, state, pool, personal, login, me)


async def _finish(message: Message, state: FSMContext, pool: WorkerPool, personal, login, me):
    """Signed in: save the account and add it to the pool (a personal one: to personal_sessions/)
    — unless it's one already known."""
    if sign_in.already_known(me, pool.storage, personal):
        await sign_in.log_out(login.client)  # the authorization just made is not needed
        await _end_login(message, state, f"ℹ️ +{me.phone} уже есть среди воркеров или личных аккаунтов — вход отменён.")
        return

    role = (await state.get_data()).get("role")
    try:
        sign_in.store(login, me, role, pool.storage, personal)
    except Exception as err:
        await sign_in.log_out(login.client)  # without its saved key the new authorization is of no use
        await _end_login(message, state, f"⚠️ Аккаунт не сохранён: {err}")
        return
    await _end_login(message, state, sign_in.added_text(login, me, role, pool.count()))
