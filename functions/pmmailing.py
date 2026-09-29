import asyncio
import json
import os
import random
from datetime import date, datetime

from telethon import functions, types
from telethon.errors import PeerIdInvalidError
from rich.prompt import Prompt, Confirm
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from functions.base import TelethonFunction
from functions.base.base import AccountLimited
from modules import parquet_db

console = Console()

STATS_PATH = os.path.join("stats", "pm_mailing.json")
LIMITS_PATH = os.path.join("stats", "account_limits.json")


class PmMailingFunc(TelethonFunction):
    """Mailing to PM (with stats)"""

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

    def load_limits(self):
        if os.path.exists(LIMITS_PATH):
            with open(LIMITS_PATH, encoding="utf-8") as fileobj:
                self.limits = json.load(fileobj)
        else:
            self.limits = {}

    def save_limits(self):
        os.makedirs("stats", exist_ok=True)

        tmp_path = LIMITS_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fileobj:
            json.dump(self.limits, fileobj, ensure_ascii=False, indent=2)

        os.replace(tmp_path, LIMITS_PATH)

    def account_sent_today(self, account_key):
        entry = self.limits.get(account_key)

        if not entry or entry["date"] != date.today().isoformat():
            return 0

        return entry["count"]

    def bump_account(self, account_key):
        today = date.today().isoformat()
        entry = self.limits.get(account_key)

        if not entry or entry["date"] != today:
            entry = {"date": today, "count": 0}
            self.limits[account_key] = entry

        entry["count"] += 1
        self.save_limits()

    @staticmethod
    def recipient_key(recipient):  # stable string key for stats/output
        if isinstance(recipient, dict):
            return recipient.get("username") or str(recipient["user_id"])

        return recipient

    async def _deliver(self, session, peer, text, media):
        if not media:
            await self.safe_call(lambda: session.send_message(peer, text))
        else:
            file = random.choice(os.listdir("media"))
            path = os.path.join("media", file)

            await self.safe_call(lambda: session.send_file(
                peer, path, caption=text, parse_mode="html"
            ))

    async def resolve_peer(self, session, recipient):
        if isinstance(recipient, dict):
            return types.InputPeerUser(recipient["user_id"], recipient["access_hash"])

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

    async def account(self, session):
        if id(session) not in self._me_cache:
            try:
                me = await session.get_me()
            except Exception as err:
                raise AccountLimited(f"get_me failed: {err}")

            if me is None:
                raise AccountLimited("account not authorized")

            self._me_cache[id(session)] = me

        return self._me_cache[id(session)]

    async def pause_between_accounts(self, account_key, name):
        if account_key in self._active_accounts:
            return

        if self._active_accounts:  # not the first account to send
            pause = self.settings.account_pause
            seconds = pause[0] if len(pause) == 1 else random.randint(*pause)

            console.print(f"[bold white]switching to {escape(name)}, pause {seconds}s[/]")
            await asyncio.sleep(seconds)

        self._active_accounts.add(account_key)

    async def send_one(self, session, recipient):
        key = self.recipient_key(recipient)
        me = await self.account(session)
        account_key = str(me.id)
        name = me.first_name or ""
        fallback = False

        if self.account_sent_today(account_key) >= self.settings.per_account_daily:
            raise AccountLimited(f"daily cap {self.settings.per_account_daily} reached")

        await self.pause_between_accounts(account_key, name)

        try:
            peer = await self.resolve_peer(session, recipient)

            try:
                await self._deliver(session, peer, self._text, self._media)
            except PeerIdInvalidError:
                if isinstance(recipient, dict) and recipient.get("username"):
                    await self._deliver(session, recipient["username"], self._text, self._media)
                    fallback = True
                else:
                    raise
        except AccountLimited:
            raise
        except Exception as err:
            console.print(
                "[{name}] [bold red]not sent.[/] {recipient} {err}"
                .format(name=escape(name), recipient=key, err=escape(str(err)))
            )
            return

        self.record_success(key)
        self.bump_account(account_key)
        console.print(
            "[{name}] [bold green]sent{via}.[/] {recipient} COUNT: [yellow]{count}[/]"
            .format(
                name=escape(name),
                via=" (via username)" if fallback else "",
                recipient=key,
                count=self.stats[key]["count"]
            )
        )

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

        databases_dir = os.path.join("assets", "databases")
        databases = sorted(
            f for f in os.listdir(databases_dir) if f.endswith(".parquet")
        ) if os.path.isdir(databases_dir) else []

        if databases:
            console.print("[bold white]databases in assets/databases/:[/]")
            for index, name in enumerate(databases):
                console.print(f"  [{index + 1}] {name}")
            console.print("[bold white]enter a number, or a path to a file[/]")

        path = Prompt.ask(
            "[bold red]file with recipients[/]",
            default=os.path.join("assets", "targets.txt")
        )

        if path.isdigit() and 1 <= int(path) <= len(databases):
            path = os.path.join(databases_dir, databases[int(path) - 1])

        if not os.path.exists(path):
            console.print("[bold red]File not found!")
            return

        if path.endswith(".parquet"):
            try:
                recipients = parquet_db.load(path)
            except Exception as err:
                console.print(f"[bold red]{err}")
                return
        else:
            with open(path, encoding="utf-8") as fileobj:
                recipients = [line.strip() for line in fileobj if line.strip()]

        if not recipients:
            console.print("[bold red]Recipients list is empty!")
            return

        self.load_stats()
        self.load_limits()

        if Confirm.ask("[bold red]skip already-messaged recipients?", default=True):
            before = len(recipients)
            recipients = [
                r for r in recipients if self.recipient_key(r) not in self.stats
            ]
            console.print(f"[bold white]skipped {before - len(recipients)} already-messaged[/]")

            if not recipients:
                console.print("[bold red]Nothing left to send!")
                return

        limit = Prompt.ask(
            "[bold red]how many recipients (blank = all)[/]",
            default=""
        )

        if limit:
            recipients = recipients[:int(limit)]

        console.print("[bold white]first recipients:[/]")
        for recipient in recipients[:5]:
            console.print("  " + self.recipient_key(recipient))

        if not Confirm.ask(f"[bold red]send to {len(recipients)} recipients?"):
            return

        media = Confirm.ask("[bold red]media")
        text = console.input("[bold red]text> [/]")

        delay = Prompt.ask(
            "[bold red]delay[/]",
            default="-".join(str(x) for x in self.settings.delay)
        )

        self.settings.delay = self.parse_delay(delay)

        self._text = text
        self._media = media
        self._me_cache = {}
        self._active_accounts = set()

        await self.run_with_rotation(recipients, self.send_one)

        self.print_stats()
