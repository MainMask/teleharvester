import asyncio
import json
import os
import random
from datetime import datetime

from telethon import functions, types
from rich.prompt import Prompt, Confirm
from rich.console import Console
from rich.table import Table

from functions.base import TelethonFunction
from functions.base.base import AccountLimited

console = Console()

STATS_PATH = os.path.join("stats", "pm_mailing.json")


class PmMailingFunc(TelethonFunction):
    """Mailing to PM (with stats)"""

    @staticmethod
    def chunkify(lst, n):  # split list
        return [lst[i::n] for i in range(n)]

    def load_stats(self):
        if os.path.exists(STATS_PATH):
            with open(STATS_PATH, encoding="utf-8") as fileobj:
                self.stats = json.load(fileobj)
        else:
            self.stats = {}

    def save_stats(self):
        os.makedirs("stats", exist_ok=True)

        tmp_path = STATS_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fileobj:
            json.dump(self.stats, fileobj, ensure_ascii=False, indent=2)

        os.replace(tmp_path, STATS_PATH)

    def record_success(self, recipient):
        entry = self.stats.setdefault(recipient, {"count": 0})
        entry["count"] += 1
        entry["last_date"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        self.save_stats()

    async def resolve_peer(self, session, recipient):
        if recipient.startswith("+") or recipient.lstrip("+").isdigit():
            result = await session(functions.contacts.ImportContactsRequest(
                contacts=[types.InputPhoneContact(
                    client_id=random.randrange(-2**63, 2**63),
                    phone=recipient,
                    first_name='contact',
                    last_name=''
                )]
            ))

            return result.users[0]

        return recipient

    async def send(self, session, recipients, text, media):
        async with self.storage.ainitialize_session(session):
            try:
                me = await session.get_me()
            except Exception:
                return

            for recipient in recipients:
                try:
                    peer = await self.resolve_peer(session, recipient)

                    if not media:
                        await self.safe_call(lambda: session.send_message(peer, text))
                    else:
                        file = random.choice(os.listdir("media"))
                        path = os.path.join("media", file)

                        await self.safe_call(lambda: session.send_file(
                            peer, path, caption=text, parse_mode="html"
                        ))
                except AccountLimited as err:
                    console.print(
                        "[{name}] [bold red]rate limit, stopping.[/] {err}"
                        .format(name=me.first_name, err=err)
                    )
                    break
                except Exception as err:
                    console.print(
                        "[{name}] [bold red]not sent.[/] {recipient} {err}"
                        .format(name=me.first_name, recipient=recipient, err=err)
                    )
                else:
                    self.record_success(recipient)
                    console.print(
                        "[{name}] [bold green]sent.[/] {recipient} COUNT: [yellow]{count}[/]"
                        .format(
                            name=me.first_name,
                            recipient=recipient,
                            count=self.stats[recipient]["count"]
                        )
                    )
                finally:
                    await self.delay()

    def print_stats(self):
        table = Table()

        table.add_column("Recipient", justify="left", style="white")
        table.add_column("Count", justify="center", style="white")
        table.add_column("Last date", style="white")

        for recipient, entry in self.stats.items():
            table.add_row(
                recipient, str(entry["count"]), entry.get("last_date", "N/A")
            )

        console.print(table)

    async def execute(self):
        self.ask_accounts_count()

        path = Prompt.ask(
            "[bold red]file with recipients[/]",
            default=os.path.join("assets", "targets.txt")
        )

        if not os.path.exists(path):
            console.print("[bold red]File not found!")
            return

        with open(path, encoding="utf-8") as fileobj:
            recipients = [line.strip() for line in fileobj if line.strip()]

        if not recipients:
            console.print("[bold red]Recipients list is empty!")
            return

        media = Confirm.ask("[bold red]media")
        text = console.input("[bold red]text> [/]")

        delay = Prompt.ask(
            "[bold red]delay[/]",
            default="-".join(str(x) for x in self.settings.delay)
        )

        self.settings.delay = self.parse_delay(delay)

        self.load_stats()

        chunks = self.chunkify(recipients, len(self.sessions))

        await asyncio.gather(*[
            self.send(session, chunk, text, media)
            for session, chunk in zip(self.sessions, chunks)
        ])

        self.print_stats()
