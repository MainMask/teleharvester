from telethon.tl.functions.contacts import AddContactRequest
from telethon.tl.types import InputUser
from rich.prompt import Prompt

from functions.base import TelethonFunction
from functions.base.base import AccountLimited, console_report
from modules import parquet_db


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
            await self._report(f"skip user_id={row.get('user_id')}: {err}")
            return

        self.added += 1
        await self._report(f"added. user_id={row['user_id']} total: {self.added}")

    async def run(self, path, delay, report):
        self._report = report
        self.delay_range = delay
        self.added = 0

        try:
            rows = parquet_db.load(path)
        except Exception as err:
            await report(str(err))
            return

        if not rows:
            await report("Database is empty!")
            return

        processed = await self.run_with_rotation(rows, self.add_one)

        await report(f"Done. Added {self.added} contacts.")
        if processed < len(rows):
            await report(
                f"Внимание: обработано {processed}/{len(rows)} — аккаунты исчерпаны, "
                "остаток пропущен."
            )

    async def execute(self):
        self.ask_accounts_count()

        path = Prompt.ask(
            "[bold red]path to .parquet[/]",
            default="assets/contacts.parquet"
        )

        delay = Prompt.ask(
            "[bold red]delay[/]",
            default="-".join(str(x) for x in self.settings.delay)
        )

        await self.run(path, self.parse_delay(delay), console_report)
