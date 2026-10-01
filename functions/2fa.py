import asyncio

from rich.console import Console

from telethon import TelegramClient
from functions.base import TelethonFunction
from functions.base.base import console_report

console = Console()

class SetPasswordFunc(TelethonFunction):
    """Set two-step verification password to accounts"""

    async def edit_2fa(self, session: TelegramClient, password: str, report):
        async with self.storage.ainitialize_session(session):
            try:
                me = await session.get_me()
            except Exception as err:
                await report(f"get_me failed: {err}")
                return

            try:
                await session.edit_2fa(new_password=password)
            except Exception as err:
                await report(f"[{me.first_name}] : Password not changed. Error: {err}")
            else:
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

