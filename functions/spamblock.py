import asyncio
import os
import re

from typing import Dict, List

from rich.prompt import Confirm

from telethon.errors import YouBlockedUserError
from telethon.sync import TelegramClient
from telethon.tl.functions.contacts import UnblockRequest

from functions.base import TelethonFunction
from functions.base.base import console_report


class SpamBlockFunc(TelethonFunction):
    """Check accounts status"""

    async def check(self, session: TelegramClient, report):
        async with self.storage.ainitialize_session(session):
            try:
                async with session.conversation("SpamBot") as conv:
                    await conv.send_message("/start")
                    response = await conv.get_response()
            except YouBlockedUserError:
                await session(UnblockRequest("spambot"))
                return await self.check(session, report)

            except Exception as err:
                await report(f"[!] {err}")
                return

            text = response.message
            lines = text.split("\n")

            if len(lines) == 1:
                await report("[+] Account active (no restriction)")

            else:
                result = re.findall(r"\d+\s\w+\s\d{4}", text)

                if not result:
                    await report("[-] Account permanently restricted")
                    return "permanent", session
                else:
                    date = result[0]
                    await report(f"[-] Account restricted until: {date}")
                    return result[0], session

    async def scan(self, report) -> Dict[str, List[TelegramClient]]:
        """Check every worker against @SpamBot; return {restriction_date: [sessions]}."""
        blocks: Dict[str, List[TelegramClient]] = {}

        results = await asyncio.gather(*[
            self.check(session, report)
            for session in self.sessions
        ])

        for result in results:
            if result is None:
                continue

            date, session = result
            blocks.setdefault(date, []).append(session)

        return blocks

    def move_restricted(self, blocks: Dict[str, List[TelegramClient]]):
        """Move restricted sessions into sessions/restricted/<date>/ (local filesystem op)."""
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

    async def run(self, report, move_restricted: bool = False) -> Dict[str, List[TelegramClient]]:
        blocks = await self.scan(report)

        if move_restricted:
            self.move_restricted(blocks)

        return blocks

    async def execute(self):
        self.ask_accounts_count()

        blocks = await self.scan(console_report)

        if Confirm.ask("[bold magenta]Move restricted sessions to other folders?[/]"):
            self.move_restricted(blocks)

