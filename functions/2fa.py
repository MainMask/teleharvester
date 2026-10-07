
from modules import tdata_import
from modules.console import console

from telethon import TelegramClient
from functions.base import TelethonFunction
from functions.base.base import console_report


class SetPasswordFunc(TelethonFunction):
    """Set two-step verification password to accounts"""

    async def edit_2fa(self, session: TelegramClient, password: str, report):
        async with self.storage.ainitialize_session(session):
            try:
                me = await self.get_me(session)
            except Exception as err:
                self.progress_failed()
                await report(f"не удалось опросить аккаунт: {err}")
                return

            path = self.storage.get_session_path(session)
            json_session = self.storage.jsessions_paths.get(path) if path else None
            current_password = json_session.account.password if json_session else None

            try:
                await session.edit_2fa(
                    current_password=current_password,
                    new_password=password,
                )
            except Exception as err:
                self.progress_failed()
                await report(f"[{me.first_name}] пароль не изменён: {err}")
            else:
                if json_session is not None:  # persist it, so a later change knows the current one
                    json_session.account.password = password
                    json_session.account.save(path)
                self.mark_done(session)
                self.progress_ok()
                await report(f"[{me.first_name}] пароль изменён")

                if json_session is not None:  # and next to the tdata, for a re-import
                    try:
                        tdata_import.write_2fa_password(json_session.account.account.phone_number, password)
                    except Exception as err:
                        await report(f"[{me.first_name}] пароль не сохранён в tdata_import: {err}")

    async def run(self, password: str, report):
        await self.run_sequential(
            lambda session, report: self.edit_2fa(session, password, report),
            report,
        )

    async def execute(self):
        self.ask_workers()

        password = console.input("[bold red]новый пароль> [/]")

        with console.status("Установка пароля..."):
            await self.run(password, console_report)

