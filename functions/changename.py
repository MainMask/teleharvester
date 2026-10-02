import asyncio
import random

from typing import List, Tuple, Optional
from telethon import TelegramClient
from telethon.tl.functions.account import UpdateProfileRequest
from modules.console import console
from functions.base import TelethonFunction
from functions.base.base import console_report


class ChangeNameFunc(TelethonFunction):
    """Change names"""
    
    @staticmethod
    def get_random_name(names: List[str]) -> Tuple[str, Optional[str]]:
        name = random.choice(names).split()

        if len(name) == 1:
            return name[0], None

        return name[0], " ".join(name[1:])
    
    async def change_name(
        self,
        session: TelegramClient,
        report,
        names: Optional[List[str]] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None
    ):
        if names is not None:
            first_name, last_name = self.get_random_name(names)

        async with self.storage.ainitialize_session(session):
            try:
                me = await session.get_me()
            except Exception as err:
                await report(f"get_me failed: {err}")
                return

            full_name = (me.first_name or "") + (" " + me.last_name if me.last_name else "")

            try:
                await session(
                    UpdateProfileRequest(
                        first_name=first_name,
                        last_name=last_name or ""
                    )
                )
            except Exception as error:
                await report(f"[!] {error}")
            else:
                new_name = " ".join(p for p in (first_name, last_name) if p)
                await report(f"Name changed successfully. ( {full_name} → {new_name} )")

    async def run(self, report, names=None, first_name=None, last_name=None):
        await asyncio.gather(*[
            self.change_name(
                session, report,
                names=names, first_name=first_name, last_name=last_name
            )
            for session in self.sessions
        ])

    async def execute(self):
        self.ask_accounts_count()

        from_file = console.input("[bold red]from file? (y/n)> ")

        if from_file == "y":
            try:
                with open("assets/names.txt") as file:
                    names = file.read().strip().splitlines()
            except FileNotFoundError:
                console.print("[bold red]File assets/names.txt not found!")
                return

            if not names:
                console.print("[bold red]Names list is empty!")
                return

            await self.run(console_report, names=names)

        else:
            name = console.input("[bold red]name> [/]").split(maxsplit=1)

            while not name:
                name = console.input("[bold red]name> [/]").split(maxsplit=1)

            print()

            first_name = name[0]
            last_name = name[1] if len(name) == 2 else None

            await self.run(console_report, first_name=first_name, last_name=last_name)

