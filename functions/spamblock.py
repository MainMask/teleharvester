import asyncio
import os
import re

from typing import Dict, List

from rich.prompt import Confirm
from rich.console import Console

from telethon.errors import YouBlockedUserError
from telethon.sync import TelegramClient
from telethon.tl.functions.contacts import UnblockRequest

from functions.base import TelethonFunction

console = Console()


class SpamBlockFunc(TelethonFunction):
    """Check accounts status"""

    async def check(self, session: TelegramClient):
        async with self.storage.ainitialize_session(session):
            try:
                async with session.conversation("SpamBot") as conv:
                    await conv.send_message("/start")
                    response = await conv.get_response()
            except YouBlockedUserError:
                await session(UnblockRequest("spambot"))
                return await self.check(session)

            except Exception as err:
                console.print(f"[bold red][!] {err}[/]")
                return

            text = response.message
            lines = text.split("\n")
            
            if len(lines) == 1:
                console.print("[bold green][+] Account active (no restriction)[/]")

            else:
                result = re.findall(r"\d+\s\w+\s\d{4}", text)

                if not result:
                    console.print(f"[bold red][-] Account permanently restricted[/]")
                    return "permanent", session
                else:
                    date = result[0]
                    console.print(f"[bold red][-] Account restricted until: {date}[/]")
                    return result[0], session 

    async def execute(self):
        blocks: Dict[str, List[TelegramClient]] = {}

        results = await asyncio.gather(*[
            self.check(session)
            for session in self.sessions
        ])

        for result in results:
            if result is None:
                continue

            date, session = result

            if not blocks.get(date):
                blocks[date] = []

            blocks[date].append(session)

        move_sessions = Confirm.ask("[bold magenta]Move restricted sessions to other folders?[/]")

        if move_sessions:
            if not os.path.exists("sessions/restricted"):
                os.mkdir("sessions/restricted")

            for date, sessions in blocks.items():
                for session in sessions:
                    path = os.path.join("sessions", "restricted", date)

                    if not os.path.exists(path):
                        os.mkdir(path)

                    session_path = self.storage.get_session_path(session)
                    session_name = os.path.basename(session_path)

                    os.rename(
                        session_path,
                        os.path.join(path, session_name)
                    )

