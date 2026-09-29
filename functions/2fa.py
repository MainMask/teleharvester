import asyncio

from rich.console import Console

from telethon import TelegramClient
from functions.base import TelethonFunction

console = Console()

class SetPasswordFunc(TelethonFunction):
    """Set two-step verification password to accounts"""
    
    async def edit_2fa(self, session: TelegramClient, password: str):
        async with self.storage.ainitialize_session(session):
            me = await session.get_me()

            try:
                await session.edit_2fa(new_password=password)
            except Exception as err:
                console.print(
                    "[{name}] : [bold red]Password not changed[/]. Error: {error}"
                    .format(name=self.safe(me.first_name), error=self.safe(err))
                )
            else:
                console.print(
                    "[{name}] : [bold green]Successfully updated password"
                    .format(name=self.safe(me.first_name))
                )

    async def execute(self):
        password = console.input("[bold red]new password> [/]")

        with console.status("Setting password..."):
            await asyncio.gather(*[
                self.edit_2fa(session=session, password=password)
                for session in self.sessions
            ])

