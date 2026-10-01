from telethon.tl.functions.contacts import AddContactRequest
from telethon.tl.types import InputUser
from rich.prompt import Prompt
from rich.console import Console

from functions.base import TelethonFunction
from functions.base.base import AccountLimited
from modules import parquet_db

console = Console()


class AddContactsFunc(TelethonFunction):
    """Add users to contacts from a .parquet database"""

    async def add_one(self, session, row):
        try:
            await self.safe_call(lambda: session(AddContactRequest(
                id=InputUser(row["user_id"], row["access_hash"]),
                first_name=row.get("first_name") or "contact",
                last_name=row.get("last_name") or "",
                phone=row.get("phone") or ""
            )))
        except AccountLimited:
            raise
        except Exception as err:
            console.print(f"[bold red]skip[/] user_id={row.get('user_id')}: {err}")
            return

        self.added += 1
        console.print(
            "[bold green]added.[/] user_id=[yellow]{uid}[/] total: [yellow]{n}[/]"
            .format(uid=row["user_id"], n=self.added)
        )

    async def execute(self):
        self.ask_accounts_count()

        path = Prompt.ask(
            "[bold red]path to .parquet[/]",
            default="assets/contacts.parquet"
        )

        try:
            rows = parquet_db.load(path)
        except Exception as err:
            console.print(f"[bold red]{err}")
            return

        if not rows:
            console.print("[bold red]Database is empty!")
            return

        delay = Prompt.ask(
            "[bold red]delay[/]",
            default="-".join(str(x) for x in self.settings.delay)
        )

        self.delay_range = self.parse_delay(delay)

        self.added = 0

        await self.run_with_rotation(rows, self.add_one)

        console.print(f"[bold green]Done. Added {self.added} contacts.")
