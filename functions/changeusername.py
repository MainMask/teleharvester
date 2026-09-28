import asyncio
import random

from telethon.tl.functions.account import UpdateUsernameRequest, CheckUsernameRequest
from rich.console import Console

from functions.base import TelethonFunction

console = Console()


class ChangeUsernameFunc(TelethonFunction):
    """Change usernames"""

    async def generate_username(self, session, base):
        for _ in range(5):
            candidate = f"{base}_{random.randint(1000, 999999)}"

            try:
                available = await session(CheckUsernameRequest(candidate))
            except Exception:
                continue

            if available:
                return candidate

        return None

    async def change(self, session, username=None, base=None):
        async with self.storage.ainitialize_session(session):
            me = await session.get_me()

            if base is not None:
                username = await self.generate_username(session, base)

                if not username:
                    console.print(f"[{me.first_name}] [bold red]couldn't find a free username[/]")
                    return

            try:
                await session(UpdateUsernameRequest(username))
            except Exception as err:
                console.print(f"[{me.first_name}] [bold red]not changed:[/] {err}")
            else:
                console.print(f"[{me.first_name}] [bold green]username set:[/] @{username}")

    async def execute(self):
        self.ask_accounts_count()

        from_file = console.input("[bold red]from file? (y/n)> ")

        if from_file == "y":
            try:
                with open("assets/usernames.txt", encoding="utf-8") as file:
                    usernames = [line.strip() for line in file if line.strip()]
            except FileNotFoundError:
                console.print("[bold red]File assets/usernames.txt not found!")
                return

            if not usernames:
                console.print("[bold red]Usernames list is empty!")
                return

            await asyncio.gather(*[
                self.change(session, username=username)
                for session, username in zip(self.sessions, usernames)
            ])
        else:
            base = console.input("[bold red]base username> [/]")

            await asyncio.gather(*[
                self.change(session, base=base)
                for session in self.sessions
            ])
