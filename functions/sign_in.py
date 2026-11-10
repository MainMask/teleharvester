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
from modules import instance_lock, login as sign_in, projects, session_import
from modules.tdata_import import IMPORT_DIR, import_all
from modules.config import load_env
from modules.console import console
from modules.settings import Settings
from modules import scraper_creds
from modules.registry import title
from modules.scraper_creds import find_account, personal_storage, pick_session, scrape_accounts


def say(text: str):
    console.print(text, markup=False, highlight=False)  # a name or an error may hold "[...]"


def pick_project(path: str | None = None) -> "projects.Project | None":
    """The project to work in, asked by number (the only one without asking; with `path`, a worker's file, its
    own; a personal account's: any, it is every project's); None without any, or if the projects' file can't be
    read (said why)."""
    try:
        found = projects.load()
    except ValueError as err:
        say(f"⚠️ {err}")
        return None
    if not found:
        say("Проектов нет — создайте проект в меню: 📁 Проекты.")
        return None
    folder = os.path.dirname(os.path.normpath(path)) if path else None
    if own := next((project for project in found if os.path.normpath(projects.sessions_dir(project)) == folder),
                   None):
        return own
    if folder == os.path.normpath(scraper_creds.PERSONAL_DIR):
        return found[0]
    if len(found) == 1:
        return found[0]
    for index, project in enumerate(found, 1):
        say(f"  [{index}] 📁 {project.name}")
    choice = console.input("[bold white]Проект (номер) >> [/]").strip()
    if choice.isdigit() and 0 < int(choice) <= len(found):
        return found[int(choice) - 1]
    say("Нет такого проекта.")
    return None


def run_standalone(func_class, *args):
    """sessions/add_session.py and login.py: run from the root, on the project asked for (login.py's file: its own).
    Not while the bot runs: the same sessions from two processes."""
    instance_lock.hold()
    load_env()
    Settings.ensure_config()
    settings = Settings()
    projects.migrate()
    project = pick_project(*args)  # login.py's file: the project it is in
    if project is None:
        return
    storage = projects.storage(project, settings.api_id, settings.api_hash)
    try:
        asyncio.run(func_class(storage, settings).execute(*args))
    except KeyboardInterrupt:  # the run's own finally has closed its client
        say("\nОтменено.")


class AddByPhoneFunc(TelethonFunction):
    """Add an account"""

    async def execute(self):
        """As the bot's ➕ Добавить: by phone, a worker of the project or a personal account; the
        files (tdata_import/, session_import/); or an account in no project."""
        say(f"[1] {title('byphone')}: в проект, для рассылок и задач\n"
            f"[2] {title('personal')}: общий для всех проектов, только для скрапа (personal_sessions/)\n"
            f"[3] {title('files')} (папки tdata в {IMPORT_DIR}/, файлы .session в {session_import.IMPORT_DIR}/)\n"
            f"[4] {title('free')} ({projects.FREE_DIR}/: ещё ни в одном проекте)")
        choice = console.input("[bold white]>> [/]").strip()
        if choice == "3":
            await self.import_files()
            return
        if choice == "4":
            await self.take_free()
            return
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
                # as the code's request: a dead proxy must not hang the prompt
                return await asyncio.wait_for(login.client.sign_in(login.phone, code), sign_in.FETCH_TIMEOUT * 2)
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
                say(f"⚠️ Вход не удался: {str(err) or 'Telegram не ответил вовремя'}")
                return None

    async def password(self, login):
        say("🔐 У аккаунта включён пароль 2FA.")
        while True:
            password = getpass.getpass("Пароль 2FA (пусто — отмена): ")  # as typed: spaces may be part of it
            if not password:
                say("Отменено.")
                return None
            try:
                me = await asyncio.wait_for(login.client.sign_in(password=password), sign_in.FETCH_TIMEOUT * 2)
            except errors.PasswordHashInvalidError:
                say("Неверный пароль.")
                continue
            except errors.FloodWaitError as err:
                say(f"⚠️ Слишком много попыток, подождите {err.seconds} с и введите пароль снова.")
                continue
            except Exception as err:
                say(f"⚠️ Вход не удался: {str(err) or 'Telegram не ответил вовремя'}")
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
            sign_in.store(login, me, role, self.storage, personal)
        except Exception as err:
            await sign_in.log_out(login.client)  # without its saved key the new authorization is of no use
            say(f"⚠️ Аккаунт не сохранён: {err}")
            return
        say(sign_in.added_text(login, me, role, len(self.storage)))

    async def take_free(self):
        """Free accounts (no project's) become the project's workers, as the bot's 📦 Из свободных."""
        free = [path for path in projects.account_files(projects.FREE_DIR) if path.endswith(".jsession")]
        if not free:
            say(f"Свободных аккаунтов нет — все уже в проектах (свободные — файлы .jsession в {projects.FREE_DIR}/).")
            return
        for index, path in enumerate(free, 1):
            say(f"[{index}] {projects.account_label(path)}")
        raw = console.input("[bold white]номера (1,3,5-8), all — все (Enter — назад)> [/]")
        added = 0
        for path in [free[index] for index in self.parse_picks(raw, len(free))]:
            try:
                path = projects.move_account(path, self.storage.directory)
            except (ValueError, OSError) as err:
                say(f"{projects.account_label(path)}: не добавлен — {err}")
                continue
            self.storage.add_jsession(path)
            added += 1
        if added:
            say(f"✅ Добавлено в проект: {added}. Воркеров: {len(self.storage)}.")

    async def import_files(self):
        """As the bot's 📥 Из файлов: the tdata_import/ folders and the .session (+ .json) files in
        session_import/ become the project's workers and join its pool, as an account added by phone does."""
        known = set(self.storage.full_sessions)
        try:
            await import_all(IMPORT_DIR, self.storage.directory, projects.proxies_path(self.storage.home))
        except Exception as err:  # e.g. too few proxies for the new accounts: no tdata is converted
            say(f"⚠️ Импорт tdata не удался: {err}")
        for path in projects.account_files(self.storage.directory):  # tdata's, in no live pool yet
            if path.endswith(".jsession") and path not in known:
                self.storage.add_jsession(path)
        _, failed = await session_import.import_all(session_import.IMPORT_DIR, self.storage)
        added = [path for path in self.storage.full_sessions if path not in known]
        for path in added:
            say(f"✅ {projects.account_label(path)}")
        for name, why in failed:
            say(f"⚠️ {name} — {why}")
        if not added and not failed:
            say(f"Нечего импортировать: положите папку tdata аккаунта в {IMPORT_DIR}/<имя>/tdata (пароль 2FA — в .txt "
                f"рядом) или файлы .session с .json того же имени в {session_import.IMPORT_DIR}/. Уже "
                "импортированные пропускаются.")
            return
        say(f"Добавлено воркеров: {len(added)}. Воркеров в проекте: {len(self.storage)}.")


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
                say(f"Аккаунт {path} не найден ни в проекте, ни в personal_sessions/.")
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
