import asyncio
import random

from telethon.tl.functions.account import UpdateUsernameRequest, CheckUsernameRequest
from modules.console import console

from functions.base import TelethonFunction
from functions.base.base import console_report


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

    async def change(self, session, report, username=None, base=None):
        async with self.storage.ainitialize_session(session):
            me = await session.get_me()

            if base is not None:
                username = await self.generate_username(session, base)

                if not username:
                    await report(f"[{me.first_name}] couldn't find a free username")
                    return

            try:
                await session(UpdateUsernameRequest(username))
            except Exception as err:
                await report(f"[{me.first_name}] not changed: {err}")
            else:
                await report(f"[{me.first_name}] username set: @{username}")

    async def run(self, report, usernames=None, base=None):
        if usernames is not None:
            await asyncio.gather(*[
                self.change(session, report, username=username)
                for session, username in zip(self.sessions, usernames)
            ])
        else:
            await asyncio.gather(*[
                self.change(session, report, base=base)
                for session in self.sessions
            ])

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

            await self.run(console_report, usernames=usernames)
        else:
            base = console.input("[bold red]base username> [/]")

            await self.run(console_report, base=base)
