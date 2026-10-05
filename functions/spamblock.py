import os
import re

from typing import Dict, List

from rich.prompt import Confirm

from telethon.errors import YouBlockedUserError
from telethon.sync import TelegramClient
from telethon.tl.functions.contacts import UnblockRequest

from functions.base import TelethonFunction
from functions.base.base import console_report
from modules import contacts_ledger, restricted_workers


class SpamBlockFunc(TelethonFunction):
    """Check accounts status"""

    async def check(self, session: TelegramClient, report):
        async with self.storage.ainitialize_session(session):
            try:
                me = await session.get_me()
            except Exception as err:
                await report(f"[!] get_me failed: {err}")
                return

            if me is None:  # get_me() swallows the auth error of a banned / logged-out account
                path = self.storage.get_session_path(session)
                name = os.path.basename(path) if path else "?"
                await report(f"[-] [{name}] Session is dead (banned or logged out) → moved to sessions/inactive")
                return "dead", session

            self.storage.remember_username(session, me.username)  # orders the pool
            self.storage.remember_name(session, me.first_name, me.last_name)
            # workers often share one display name: the username tells them apart
            who = f"@{me.username}" if me.username else me.first_name

            try:
                async with session.conversation("SpamBot") as conv:
                    await conv.send_message("/start")
                    response = await conv.get_response()
            except YouBlockedUserError:
                try:
                    await session(UnblockRequest("spambot"))
                except Exception as err:
                    await report(f"[!] [{who}] can't unblock @SpamBot: {err}")
                    return
                return await self.check(session, report)

            except Exception as err:
                await report(f"[!] [{who}] {err}")
                return

            text = response.message
            lines = text.split("\n")

            if len(lines) == 1:
                await report(f"[+] [{who}] Account active (no restriction)")
                return "active", session

            else:
                result = re.findall(r"\d+\s\w+\s\d{4}", text)

                if not result:
                    people = sum(1 for worker in contacts_ledger.load().values() if worker == me.id)
                    await report(
                        f"[-] [{who}] Account permanently restricted → excluded from mailing/contacts; "
                        f"its {people} people go to other workers"
                    )
                    return "permanent", session
                else:
                    date = result[0]
                    await report(f"[-] [{who}] Account restricted until: {date}")
                    return result[0], session

    async def scan(self, report) -> Dict[str, List[TelegramClient]]:
        """Check every worker against @SpamBot; return {restriction_date: [sessions]}
        ("permanent" and "dead" are dates too). Rewrites the list of permanently
        restricted workers: a worker that could not be checked keeps its entry."""
        blocks: Dict[str, List[TelegramClient]] = {}

        results = await self.gather_in_order(self.check, report)

        excluded = set(restricted_workers.load())

        for result in results:
            if result is None:
                continue

            date, session = result
            path = self.storage.get_session_path(session)
            if path is None:  # already moved/forgotten
                pass
            elif date == "permanent":
                excluded.add(path)
            else:
                excluded.discard(path)

            if date != "active":
                blocks.setdefault(date, []).append(session)

        restricted_workers.save(sorted(excluded))
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
                if session_path is None:  # already moved/forgotten: nothing to relocate
                    continue
                session_name = os.path.basename(session_path)

                os.rename(
                    session_path,
                    os.path.join(path, session_name)
                )
                self.storage._forget_session(session_path)  # no stale path for a later move

    async def drop_dead(self, blocks: Dict[str, List[TelegramClient]]) -> List[TelegramClient]:
        """Take the dead sessions out of `blocks` and the pool (to sessions/inactive): their
        people become everyone's at once. Returns them."""
        dead = blocks.pop("dead", [])
        for session in dead:
            path = self.storage.get_session_path(session)
            if path is not None:
                await session.disconnect()  # CLI clients stay connected: a forgotten one runs on
                self.storage.move_to_inactive(path)
        return dead

    async def run(self, report, move_restricted: bool = False) -> Dict[str, List[TelegramClient]]:
        blocks = await self.scan(report)
        await self.drop_dead(blocks)

        if move_restricted:
            self.move_restricted(blocks)

        return blocks

    async def execute(self):
        self.ask_accounts_count()

        blocks = await self.run(console_report)

        if Confirm.ask("[bold magenta]Move restricted sessions to other folders?[/]"):
            self.move_restricted(blocks)

