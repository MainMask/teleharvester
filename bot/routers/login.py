"""🤖 Воркеры → 📲 Добавить по номеру and 🔑 Код входа (the logic: bot/services/login.py)."""

import asyncio
import html
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from telethon import errors
from telethon.tl.types.auth import SentCodeTypeApp

from bot.callbacks import ChoiceCB, MenuAction, MenuCB
from bot.keyboards.common import choice_kb
from bot.keyboards.menu import main_menu
from bot.services import login as sign_in
from bot.services.delegation import WorkerPool
from bot.states import AddByPhone
from modules import tdata_import
from modules.scraper_creds import find_account, scrape_accounts

router = Router()

CODE_PROMPT = (
    "Пришлите код <b>через пробелы или дефисы</b>, например <code>1 2 3 4 5</code>: код, "
    "отправленный в Telegram целиком, Telegram аннулирует."
)
RESEND_KB = choice_kb("phone_resend", [("📨 Отправить код заново", "resend")])


# =============================== login codes ===============================

def _codes_text(account, codes, now: datetime) -> str:
    lines = [f"🔑 <b>Коды входа</b> — {html.escape(account.label)}", ""]
    if not codes:
        lines.append(f"За последние {sign_in.CODES_WINDOW.seconds // 60} мин кодов нет. "
                     "Запросите вход на устройстве и нажмите 🔄 Обновить.")
    for code, sent_at in codes:
        minutes = int((now - sent_at).total_seconds() // 60)
        lines.append(f"<code>{sign_in.spaced(code)}</code> · {'только что' if minutes < 1 else f'{minutes} мин назад'}")
    return "\n".join(lines)


async def _codes(pool: WorkerPool, account, index: int) -> tuple[str, object]:
    """(text, keyboard) of a worker's recent login codes."""
    refresh = choice_kb("codes_refresh", [("🔄 Обновить", str(index))])
    if pool.scraping is not None and pool.scraping.path == account.path:
        # its key is in use by the scrape's own client: a second connection risks AUTH_KEY_DUPLICATED
        return f"⏳ {html.escape(account.label)} занят скрапом — коды после его окончания.", refresh
    try:
        codes = await sign_in.fetch_codes(pool, account.client, account.path)
    except Exception as err:  # a dead proxy, a logged-out session: a timeout has no text
        return f"⚠️ {html.escape(account.label)}: не удалось подключиться ({html.escape(str(err) or type(err).__name__)}).", refresh
    return _codes_text(account, codes, datetime.now(timezone.utc)), refresh


def _account_at(pool: WorkerPool, paths: list, value: str):
    index = int(value)
    return find_account(scrape_accounts(pool.storage), paths[index]) if 0 <= index < len(paths) else None


@router.callback_query(MenuCB.filter(F.action == MenuAction.CODES))
async def codes_start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool):
    await callback.answer()
    await state.clear()
    accounts = scrape_accounts(pool.storage)  # the workers alone, labelled
    if not accounts:
        await callback.message.answer("Нет воркер-аккаунтов.")
        return
    await state.update_data(code_paths=[account.path for account in accounts])  # buttons carry an index
    if len(accounts) == 1:
        text, markup = await _codes(pool, accounts[0], 0)
        await callback.message.answer(text, parse_mode="HTML", reply_markup=markup)
        return
    await callback.message.answer("Коды какого воркера показать?", reply_markup=choice_kb(
        "codes_pick", [(account.label, str(i)) for i, account in enumerate(accounts)]))


@router.callback_query(ChoiceCB.filter(F.scope.in_({"codes_pick", "codes_refresh"})))
async def codes_show(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext, pool: WorkerPool):
    await callback.answer()
    account = _account_at(pool, (await state.get_data()).get("code_paths", []), callback_data.value)
    if account is None:  # a stale button: the list changed or another flow cleared it
        await callback.message.answer("Список устарел — откройте 🔑 Код входа заново.")
        return
    text, markup = await _codes(pool, account, int(callback_data.value))
    if callback_data.scope == "codes_pick":
        await callback.message.answer(text, parse_mode="HTML", reply_markup=markup)
        return
    try:  # 🔄 updates the codes in place
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    except Exception:  # "message is not modified": nothing new
        pass


# =============================== add by phone ===============================

def _known(storage, phone: str) -> bool:
    """The number is already loaded there (stored with or without the +)."""
    return storage is not None and bool(storage.is_phone_exists(phone) or storage.is_phone_exists(f"+{phone}"))


async def _delete(message: Message):
    """Best effort: the code / password must not stay in the chat."""
    try:
        await message.delete()
    except Exception:
        pass


async def _end_login(message: Message, state: FSMContext, text: str):
    await sign_in.close(message.chat.id)
    await state.clear()
    await message.answer(text, reply_markup=main_menu())


@router.callback_query(MenuCB.filter(F.action == MenuAction.PHONE))
async def phone_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await state.set_state(AddByPhone.phone)
    await callback.message.answer(
        "Номер телефона аккаунта в международном формате, например <code>+79991234567</code>.\n"
        "Аккаунт должен быть уже зарегистрирован в Telegram. /cancel — отмена.",
        parse_mode="HTML",
    )


@router.message(AddByPhone.phone)
async def phone_input(message: Message, state: FSMContext, pool: WorkerPool, personal):
    phone = sign_in.normalize_phone(message.text)
    if phone is None:
        await message.answer("Не похоже на номер. Пришлите его в формате +79991234567:")
        return
    if _known(pool.storage, phone):
        await _end_login(message, state, f"ℹ️ Аккаунт +{phone} уже есть среди воркеров.")
        return
    if _known(personal, phone):
        await _end_login(message, state, f"⛔ +{phone} — ваш личный аккаунт (personal_sessions/): воркером он не станет.")
        return

    used = [js.account.proxy for js in pool.storage.jsessions_paths.values()]
    try:  # as the tdata import: at most ACCOUNTS_PER_PROXY accounts per proxy
        proxy = tdata_import.choose_pool_proxy(used, tdata_import.load_proxies(tdata_import.PROXIES_FILE))
    except ValueError as err:
        await _end_login(message, state, f"⚠️ {err}.")
        return

    await sign_in.close(message.chat.id)  # a sign-in started before in this chat
    client, generator = sign_in.new_client(proxy)
    async def request_code():
        await client.connect()
        return await client.send_code_request(phone)

    try:  # a dead proxy: Telethon would retry for a minute or more
        sent = await asyncio.wait_for(request_code(), sign_in.FETCH_TIMEOUT * 2)
    except Exception as err:
        try:
            await client.disconnect()
        except Exception:
            pass
        if isinstance(err, errors.FloodWaitError):
            reason = f"слишком много попыток, подождите {err.seconds} с"
        elif isinstance(err, errors.PhoneNumberInvalidError):
            reason = "неверный номер"
        elif isinstance(err, errors.PhoneNumberBannedError):
            reason = "номер заблокирован в Telegram"
        else:
            reason = str(err) or type(err).__name__
        await _end_login(message, state, f"⚠️ Код не отправлен: {reason}.")
        return

    sign_in.open_login(message.chat.id, sign_in.Login(client, phone, proxy, generator))
    await state.set_state(AddByPhone.code)
    where = "в Telegram на другом устройстве аккаунта" if isinstance(
        getattr(sent, "type", None), SentCodeTypeApp) else "по SMS или звонком"
    await message.answer(f"📨 Код отправлен {where}.\n{CODE_PROMPT}", parse_mode="HTML", reply_markup=RESEND_KB)


@router.callback_query(ChoiceCB.filter(F.scope == "phone_resend"))
async def phone_resend(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    login = sign_in.get(callback.message.chat.id)
    if login is None:
        await callback.message.answer("Вход устарел — начните заново: 🤖 Воркеры → 📲 Добавить по номеру.")
        return
    try:
        await login.client.send_code_request(login.phone)
    except errors.FloodWaitError as err:
        await callback.message.answer(f"⚠️ Слишком много попыток, подождите {err.seconds} с.")
        return
    except Exception as err:
        await callback.message.answer(f"⚠️ Код не отправлен: {err}")
        return
    await state.set_state(AddByPhone.code)
    await callback.message.answer(f"📨 Новый код отправлен.\n{CODE_PROMPT}", parse_mode="HTML")


@router.message(AddByPhone.code)
async def code_input(message: Message, state: FSMContext, pool: WorkerPool, personal):
    login = sign_in.get(message.chat.id)
    if login is None:
        await _end_login(message, state, "Вход устарел — начните заново: 🤖 Воркеры → 📲 Добавить по номеру.")
        return
    code = sign_in.code_digits(message.text)
    await _delete(message)
    if code is None:
        await message.answer(CODE_PROMPT, parse_mode="HTML")
        return

    try:
        me = await login.client.sign_in(login.phone, code)
    except errors.SessionPasswordNeededError:
        await state.set_state(AddByPhone.password)
        await message.answer("🔐 У аккаунта включена двухэтапная проверка. Пришлите пароль (сообщение удалю):")
        return
    except errors.PhoneCodeInvalidError:
        await message.answer(f"Неверный код. {CODE_PROMPT}", parse_mode="HTML")
        return
    except errors.PhoneCodeExpiredError:
        await message.answer("⌛ Код истёк (или Telegram аннулировал его, увидев целиком в сообщении). "
                             "Запросите новый и пришлите через пробелы.", reply_markup=RESEND_KB)
        return
    except errors.PhoneNumberUnoccupiedError:
        await _end_login(message, state, "⚠️ Номер не зарегистрирован в Telegram: бот добавляет только существующие аккаунты.")
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
    login = sign_in.get(message.chat.id)
    if login is None:
        await _end_login(message, state, "Вход устарел — начните заново: 🤖 Воркеры → 📲 Добавить по номеру.")
        return
    password = message.text  # as typed: spaces may be part of it
    await _delete(message)
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
    """Signed in: save the account and add it to the pool — unless it's one already known."""
    personal_ids = {js.account.account.user_id for js in personal.json_sessions} if personal is not None else set()
    if me.id in personal_ids or _known(pool.storage, me.phone):
        try:  # the authorization just made is not needed: don't leave it on the account
            await login.client.log_out()
        except Exception:
            pass
        await _end_login(message, state, f"ℹ️ +{me.phone} уже есть среди воркеров или личных аккаунтов — вход отменён.")
        return

    try:
        path = sign_in.save_account(login, me)
        pool.storage.add_jsession(path)
    except Exception as err:
        try:  # without its saved key the new authorization is of no use
            await login.client.log_out()
        except Exception:
            pass
        await _end_login(message, state, f"⚠️ Аккаунт не сохранён: {err}")
        return

    proxy = login.proxy
    await _end_login(
        message, state,
        f"✅ Аккаунт добавлен.\n"
        f"📱 Номер: +{me.phone}\n"
        f"🔐 2FA: {'да' if login.password else 'нет'}\n"
        f"🌐 Прокси: {f'{proxy.proxy_type}://{proxy.ip}:{proxy.port}' if proxy else 'без прокси'}\n"
        f"🤖 Всего воркеров: {pool.count()}",
    )
