import asyncio

from modules.console import console

from telethon import TelegramClient
from functions.base import TelethonFunction
from functions.base.base import console_report


class SetPasswordFunc(TelethonFunction):
    """Set two-step verification password to accounts"""

    async def edit_2fa(self, session: TelegramClient, password: str, report):
        async with self.storage.ainitialize_session(session):
            try:
                me = await session.get_me()
            except Exception as err:
                await report(f"get_me failed: {err}")
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
                await report(f"[{me.first_name}] : Password not changed. Error: {err}")
            else:
                if json_session is not None:  # persist it, so a later change knows the current one
                    json_session.account.password = password
                    json_session.account.save(path)
                await report(f"[{me.first_name}] : Successfully updated password")

    async def run(self, password: str, report):
        await asyncio.gather(*[
            self.edit_2fa(session, password, report)
            for session in self.sessions
        ])

    async def execute(self):
        self.ask_accounts_count()

        password = console.input("[bold red]new password> [/]")

        with console.status("Setting password..."):
            await self.run(password, console_report)

