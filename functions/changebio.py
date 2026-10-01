import asyncio
from telethon.tl.functions.account import UpdateProfileRequest
from rich.console import Console
from functions.base import TelethonFunction
from functions.base.base import console_report

console = Console()


class ChangeBioFunc(TelethonFunction):
    """Change bio"""

    async def change_bio(self, session, bio: str, report):
        async with self.storage.ainitialize_session(session):
            try:
                me = await session.get_me()
            except Exception as err:
                await report(f"get_me failed: {err}")
                return

            try:
                await session(
                    UpdateProfileRequest(about=bio)
                )
            except Exception as err:
                await report(f"[{me.first_name}] not changed: {err}")
            else:
                await report(f"[{me.first_name}] bio changed")

    async def run(self, bio: str, report):
        await asyncio.gather(*[
            self.change_bio(session, bio, report)
            for session in self.sessions
        ])

    async def execute(self):
        self.ask_accounts_count()

        bio = console.input("[bold red]bio> [/]")

        await self.run(bio, console_report)
