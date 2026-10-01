import asyncio
from telethon.tl.functions.account import UpdateProfileRequest
from rich.console import Console
from functions.base import TelethonFunction

console = Console()


class ChangeBioFunc(TelethonFunction):
    """Change bio"""
    
    async def change_bio(self, session, bio: str):
        async with self.storage.ainitialize_session(session):
            try:
                me = await session.get_me()
            except Exception as err:
                console.print(f"[bold red]get_me failed:[/] {self.safe(err)}")
                return

            try:
                await session(
                    UpdateProfileRequest(about=bio)
                )
            except Exception as err:
                console.print(f"[{self.safe(me.first_name)}] [bold red]not changed:[/] {self.safe(err)}")
            else:
                console.print(f"[{self.safe(me.first_name)}] [bold green]bio changed[/]")

    async def execute(self):
        self.ask_accounts_count()

        bio = console.input("[bold red]bio> [/]")
        
        await asyncio.gather(*[
            self.change_bio(session, bio)
            for session in self.sessions
        ])
