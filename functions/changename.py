import asyncio
import random

from typing import List, Tuple, Optional
from telethon import TelegramClient
from telethon.tl.functions.account import UpdateProfileRequest
from rich.console import Console
from functions.base import TelethonFunction

console = Console()


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
        account_index: int,
        names: Optional[List[str]] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None
    ):
        if names is not None:
            first_name, last_name = self.get_random_name(names)

        async with self.storage.ainitialize_session(session):
            me = await session.get_me()
            
            full_name = (me.first_name or "") + (" " + me.last_name if me.last_name else "")

            try:
                await session(
                    UpdateProfileRequest(
                        first_name=first_name,
                        last_name=last_name or ""
                    )
                )
            except Exception as error:
                console.print(f"[bold red][!][/] {self.safe(error)}")
            else:
                console.print(f"Name changed [bold green]successfully.[/] ( {self.safe(full_name)} → {self.safe(first_name)} {self.safe(last_name)} )")

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

            await asyncio.gather(*[
                self.change_name(session=session, account_index=index, names=names)
                for index, session in enumerate(self.sessions)
            ])

        else:
            name = console.input("[bold red]name> [/]").split(maxsplit=1)

            while not name:
                name = console.input("[bold red]name> [/]").split(maxsplit=1)

            print()

            first_name = name[0]
            last_name = name[1] if len(name) == 2 else None

            await asyncio.gather(*[
                self.change_name(
                    session=session,
                    account_index=index,
                    first_name=first_name,
                    last_name=last_name
                )
                for index, session in enumerate(self.sessions)
            ])

