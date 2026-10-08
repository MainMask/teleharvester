"""Adding an account by phone and an account's login codes, in the terminal — by the bot's
rules (modules/login.py): a worker or a personal account, no duplicates, a worker's
proxy from the pool, a personal account without a proxy and without its 2FA password."""
import asyncio
import getpass
import os
from datetime import datetime, timezone

from telethon import errors
from telethon.tl.types.auth import SentCodeTypeApp

from functions.base import TelethonFunction
from modules import instance_lock, login as sign_in
from modules.config import load_env
from modules.console import console
from modules.settings import Settings
from modules.storages.sessions_storage import SessionsStorage
from modules import scraper_creds
from modules.scraper_creds import find_account, personal_storage, pick_session, scrape_accounts


def say(text: str):
    console.print(text, markup=False, highlight=False)  # a name or an error may hold "[...]"


def run_standalone(func_class, *args):
    """sessions/add_session.py and sessions/login.py: the menu item without the menu (run from
    the root). Not while the bot or the menu runs: the same sessions from two processes."""
    instance_lock.hold()
    load_env()
    Settings.ensure_config()
    settings = Settings()
    storage = SessionsStorage("sessions", settings.api_id, settings.api_hash, initialize=False)
    try:
        asyncio.run(func_class(storage, settings).execute(*args))
    except KeyboardInterrupt:  # the run's own finally has closed its client
        say("\nОтменено.")


class AddByPhoneFunc(TelethonFunction):
    """Add an account by phone"""

    async def execute(self):
        say("[1] Воркер — в пул для рассылок и задач\n"
            "[2] Личный — только для скрапа (personal_sessions/)")
        choice = console.input("[bold white]>> [/]").strip()
        if choice not in ("1", "2"):
            say("Нет такого пункта.")
            return
        role = sign_in.PERSONAL if choice == "2" else "worker"
        os.makedirs(scraper_creds.PERSONAL_DIR, exist_ok=True)  # a personal account added here lands there
        personal = personal_storage(self.settings.api_id, self.settings.api_hash)

        phone = sign_in.normalize_phone(console.input("[bold white]номер, например +79991234567> [/]"))
        while phone is None:
            phone = sign_in.normalize_phone(console.input("[bold white]не похоже на номер, ещё раз> [/]"))
        if refused := sign_in.refusal(role, phone, self.storage, personal):
            say(refused)
            return
        try:
            proxy = sign_in.role_proxy(role, self.storage)
        except ValueError as err:
            say(f"⚠️ {err}.")
            return

        client, generator = sign_in.new_client(proxy)
        login = sign_in.Login(client, phone, proxy, generator)
        try:
            me = await self.sign_in(login)
            if me is not None:
                await self.finish(login, me, role, personal)
        finally:  # Ctrl+C too: the fresh client must not stay connected
            try:
                await client.disconnect()
            except Exception:
                pass

    async def sign_in(self, login):
        """The code (and the 2FA password) typed here; the signed-in account, or None."""
        try:
            sent = await sign_in.request_code(login.client, login.phone)
        except Exception as err:
            say(f"⚠️ Код не отправлен: {sign_in.send_error(err)}.")
            return None
        where = "в Telegram на другом устройстве аккаунта" if isinstance(
            getattr(sent, "type", None), SentCodeTypeApp) else "по SMS или звонком"
        say(f"📨 Код отправлен {where}. Пустой ввод — отправить заново, q — отмена.")

        while True:
            raw = console.input("[bold white]код> [/]").strip()
            if raw.lower() == "q":
                say("Отменено.")
                return None
            if not raw:
                try:  # as the first request: a dead proxy must not hang it
                    await asyncio.wait_for(login.client.send_code_request(login.phone), sign_in.FETCH_TIMEOUT * 2)
                except Exception as err:
                    say(f"⚠️ Код не отправлен: {sign_in.send_error(err)}.")
                else:
                    say("📨 Новый код отправлен.")
                continue
            code = sign_in.code_digits(raw)
            if code is None:
                say("Ожидаются цифры кода.")
                continue
            try:
                return await login.client.sign_in(login.phone, code)
            except errors.SessionPasswordNeededError:
                return await self.password(login)
            except errors.PhoneCodeInvalidError:
                say("Неверный код, введите ещё раз.")
            except errors.PhoneCodeExpiredError:
                say("⌛ Код истёк — пустой ввод отправит новый.")
            except errors.PhoneNumberUnoccupiedError:
                say("⚠️ Номер не зарегистрирован в Telegram: добавляются только существующие аккаунты.")
                return None
            except errors.FloodWaitError as err:
                say(f"⚠️ Слишком много попыток, подождите {err.seconds} с и введите код снова.")
            except Exception as err:
                say(f"⚠️ Вход не удался: {err}")
                return None

    async def password(self, login):
        say("🔐 У аккаунта включена двухэтапная проверка.")
        while True:
            password = getpass.getpass("Пароль 2FA (пусто — отмена): ")  # as typed: spaces may be part of it
            if not password:
                say("Отменено.")
                return None
            try:
                me = await login.client.sign_in(password=password)
            except errors.PasswordHashInvalidError:
                say("Неверный пароль.")
                continue
            except errors.FloodWaitError as err:
                say(f"⚠️ Слишком много попыток, подождите {err.seconds} с и введите пароль снова.")
                continue
            except Exception as err:
                say(f"⚠️ Вход не удался: {err}")
                return None
            login.password = password  # a worker keeps it in its .jsession (sign_in.store)
            return me

    async def finish(self, login, me, role: str, personal):
        """Signed in: save the account and add it to its storage — unless it's one already known."""
        if sign_in.already_known(me, self.storage, personal):
            await sign_in.log_out(login.client)  # the authorization just made is not needed
            say(f"ℹ️ +{me.phone} уже есть среди воркеров или личных аккаунтов — вход отменён.")
            return
        try:
            path = sign_in.store(login, me, role, self.storage, personal)
        except Exception as err:
            await sign_in.log_out(login.client)  # without its saved key the new authorization is of no use
            say(f"⚠️ Аккаунт не сохранён: {err}")
            return
        if role != sign_in.PERSONAL and self.storage.initialize:
            # the menu's workers were connected at start: so is this one, for the next items
            await self.storage.check_session(self.storage.full_sessions[path], path)
        say(sign_in.added_text(login, me, role, len(self.storage)))


class LoginCodesFunc(TelethonFunction):
    """Login codes"""

    async def execute(self, path: str | None = None):
        """path: the account's session file (sessions/login.py); asked for without it."""
        personal = personal_storage(self.settings.api_id, self.settings.api_hash)
        if path is None:
            account = pick_session(self.storage, personal)
            if account is None:
                return
        else:
            account = find_account(scrape_accounts(self.storage, personal), os.path.normpath(path))
            if account is None:
                say(f"Аккаунт {path} не найден в sessions/ и personal_sessions/.")
                return
        storage = personal if account.personal else self.storage

        async def read():
            async with storage.ainitialize_session(account.client):
                return await sign_in.read_codes(account.client)

        while True:
            try:  # a dead proxy, a logged-out session: a timeout has no text
                # connect() inside the timeout too, as the bot's fetch_codes
                codes = await asyncio.wait_for(read(), sign_in.FETCH_TIMEOUT)
            except Exception as err:
                say(f"⚠️ {account.label}: не удалось подключиться ({str(err) or type(err).__name__}).")
                return
            say(self.codes_text(account, codes, datetime.now(timezone.utc)))
            if console.input("[bold white]Обновить? (y/n) [/]").strip() != "y":
                return

    @staticmethod
    def codes_text(account, codes, now: datetime) -> str:
        lines = [f"🔑 Коды входа — {account.label}"]
        if not codes:
            lines.append(f"{sign_in.NO_CODES} и обновите.")
        for code, sent_at in codes:
            lines.append(f"{sign_in.spaced(code)} · {sign_in.ago(sent_at, now)}")
        return "\n".join(lines)
