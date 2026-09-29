import asyncio
from telethon.tl.functions.account import UpdateProfileRequest
from rich.console import Console
from functions.base import TelethonFunction

console = Console()


class ChangeBioFunc(TelethonFunction):
    """Change bio"""
    
    async def change_bio(self, session, bio: str):
        async with self.storage.ainitialize_session(session):
            me = await session.get_me()

            try:
                await session(
                    UpdateProfileRequest(about=bio)
                )
            except Exception as err:
                console.print(f"[{self.safe(me.first_name)}] [bold red]not changed:[/] {self.safe(err)}")
            else:
                console.print(f"[{self.safe(me.first_name)}] [bold green]bio changed[/]")

    async def execute(self):
        bio = console.input("[bold red]bio> [/]")
        
        await asyncio.gather(*[
            self.change_bio(session, bio)
            for session in self.sessions
        ])
