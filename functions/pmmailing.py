import asyncio
import os
from datetime import datetime

from telethon import types
from telethon.errors import PeerIdInvalidError
from rich.prompt import Prompt, Confirm
from modules.console import console
from rich.table import Table

from functions.base import TelethonFunction
from functions.base.base import SKIPPED, AccountLimited, console_report, pick_seconds
from modules import contacts_ledger, json_file, parquet_db, rich_message, scraped_files
from modules.account_limits import DailyCounter
from modules.rich_message import RichContent


STATS_PATH = os.path.join("stats", "pm_mailing.json")
LIMITS_PATH = os.path.join("stats", "account_limits.json")

# The stats ledger grows by one entry per unique recipient ever messaged and is
# rewritten in full on each save. Buffer this many successes in memory before
# flushing it (run() always flushes the tail), so a multi-day campaign doesn't
# rewrite a large file on every single send. A hard crash can then lose up to this
# many of the most recent records (at worst that many recipients re-messaged next
# run); acceptable for a dedup/stat ledger. account_limits.json stays per-send: it
# is small (pruned to today) and its per-send write guards the daily cap on a crash.
STATS_SAVE_EVERY = 25


class PmMailingFunc(TelethonFunction):
    """Mailing to PM (with stats)"""

    def load_stats(self):
        self.stats = json_file.load(STATS_PATH, {})

    def save_stats(self):
        json_file.save(STATS_PATH, self.stats)

    def record_success(self, recipient):
        entry = self.stats.setdefault(recipient, {"count": 0})
        entry["count"] += 1
        entry["last_date"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        self._unsaved += 1
        if self._unsaved >= STATS_SAVE_EVERY:
            self.flush_stats()

    def flush_stats(self):
        """Persist successes buffered since the last save (see STATS_SAVE_EVERY)."""
        if self._unsaved:
            self.save_stats()
            self._unsaved = 0

    @staticmethod
    def recipient_key(recipient):  # stable string key for stats
        if isinstance(recipient, dict):
            return str(recipient["user_id"])  # a username can change or appear; the id can't

        return recipient

    @staticmethod
    def recipient_label(recipient):  # how a recipient is shown in reports
        if isinstance(recipient, dict):
            return recipient.get("username") or str(recipient["user_id"])

        return recipient

    async def _deliver(self, session, peer):
        await rich_message.send(session, peer, self._content, self.safe_call, report=self._report)

    async def resolve_peer(self, session, recipient, foreign=False):
        if isinstance(recipient, dict):
            if foreign:  # the base's access hash belongs to another account: it can't work here
                return recipient["username"]
            return types.InputPeerUser(recipient["user_id"], recipient["access_hash"])

        if recipient.startswith("+") or recipient.lstrip("+").isdigit():
            users = await self.import_phone_contact(session, recipient)
            if not users:
                raise ValueError(f"phone {recipient} not resolved")
            return users[0]

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
            seconds = pick_seconds(self.settings.account_pause)

            await self._report(f"switching to {name}, pause {seconds}s")
            await asyncio.sleep(seconds)

        self._active_accounts.add(account_key)

    async def send_one(self, session, recipient):
        key = self.recipient_key(recipient)
        me = await self.account(session)
        account_key = str(me.id)
        name = me.first_name or ""
        foreign = isinstance(recipient, dict) and recipient.get("owner_id") not in (None, me.id)
        fallback = foreign

        if foreign and not recipient.get("username"):
            self._skipped += 1  # only the base's owner can reach them; left unsent for it
            return SKIPPED

        if self._limits.reached(account_key):
            self._capped.add(id(session))  # out for today only: its people wait for it
            raise AccountLimited(f"daily cap {self.settings.per_account_daily} reached")

        await self.pause_between_accounts(account_key, name)

        try:
            peer = await self.resolve_peer(session, recipient, foreign)

            try:
                await self._deliver(session, peer)
            except PeerIdInvalidError:
                if isinstance(recipient, dict) and recipient.get("username"):
                    await self._deliver(session, recipient["username"])
                    fallback = True
                else:
                    raise
        except AccountLimited:
            raise
        except Exception as err:
            await self._report(f"[{name}] not sent. {self.recipient_label(recipient)} {err}")
            return

        self.record_success(key)
        self._limits.bump(account_key)
        await self._report(
            "[{name}] sent{via}. {recipient} COUNT: {count}".format(
                name=name,
                via=" (via username)" if fallback else "",
                recipient=self.recipient_label(recipient),
                count=self.stats[key]["count"],
            )
        )

    def load_recipients(self, path):
        """Read recipients from a .parquet DB (dict rows) or a .txt list (strings)."""
        if path.endswith(".parquet"):
            return parquet_db.load(path)

        with open(path, encoding="utf-8") as fileobj:
            return [line.strip() for line in fileobj if line.strip()]

    def filter_unsent(self, recipients):
        """Drop recipients already recorded in stats (requires load_stats first)."""
        def sent(recipient):
            if self.recipient_key(recipient) in self.stats:
                return True
            # stats written before the user_id key keep a base's people by username
            return isinstance(recipient, dict) and bool(recipient.get("username")) \
                and recipient["username"] in self.stats

        return [r for r in recipients if not sent(r)]

    async def run(self, recipients, content, delay, report):
        self._report = report
        self._content = content
        self._me_cache = {}
        self._active_accounts = set()
        self._unsaved = 0
        self._skipped = 0
        self._capped = set()
        self.delay_range = delay
        self.progress_prepare()

        self.load_stats()
        self._limits = DailyCounter(LIMITS_PATH, self.settings.per_account_daily)  # 0: unlimited
        await self.check_workers(report)
        self.progress_total(len(recipients))

        try:
            processed = waiting = 0
            *own, (shared_sessions, shared) = self.split_queues(recipients, contacts_ledger.load())
            for (worker,), queue in own:
                if worker in self.on_hold:  # on hold (scraping, or left out of a CLI run): its people wait, as for a daily cap
                    waiting += len(queue)
                    await report(f"{len(queue)} получателей ждут своего воркера: он не участвует в этом запуске.")
                    self.progress_drop(len(queue))
                    continue
                done = await self.run_with_rotation(queue, self.send_one, [worker])
                processed += done
                rest = queue[done:]
                if not rest:
                    continue
                # it stopped short (a cap, a flood, out for good): no more sends from it this run
                shared_sessions = [s for s in shared_sessions if s is not worker]
                # a contact is that worker's own: another one would write to a stranger, so
                # its people move only when it is out for good, not for a daily cap or a wait
                if id(worker) not in self._capped and await self.worker_gone(worker, report):
                    shared = shared + rest
                    await report(f"Воркер выбыл: {len(rest)} его получателей переданы другим воркерам.")
                else:
                    waiting += len(rest)
                    await report(f"{len(rest)} получателей ждут своего воркера до следующего запуска.")
                    self.progress_drop(len(rest))  # not this run: out of the total
            processed += await self.run_with_rotation(shared, self.send_one, shared_sessions)
        finally:
            self.flush_stats()  # persist the tail on any exit (success, error, cancel)

        if self._skipped:
            await report(f"Пропущено без username (база другого аккаунта): {self._skipped}")

        if processed + waiting < len(recipients):
            await report(
                f"Внимание: обработано {processed}/{len(recipients)} — аккаунты исчерпаны, "
                "остаток не отправлен."
            )
        else:
            await report(f"Done. Recipients processed: {processed}")

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

        path = self.ask_file(
            "[bold red]file with recipients[/]",
            scraped_files.participant_bases(),  # the scraped bases, newest first, as in the bot
            default=os.path.join("assets", "targets.txt"),
        )

        if not os.path.exists(path):
            console.print("[bold red]File not found!")
            return

        try:
            recipients = self.load_recipients(path)
        except Exception as err:
            console.print(f"[bold red]{err}")
            return

        if not recipients:
            console.print("[bold red]Recipients list is empty!")
            return

        self.load_stats()

        if Confirm.ask("[bold red]skip already-messaged recipients?", default=True):
            before = len(recipients)
            recipients = self.filter_unsent(recipients)
            console.print(f"[bold white]skipped {before - len(recipients)} already-messaged[/]")

            if not recipients:
                console.print("[bold red]Nothing left to send!")
                return

        while True:
            limit = Prompt.ask(
                "[bold red]how many recipients (blank = all)[/]",
                default=""
            )

            if not limit:
                break

            if limit.isdigit() and int(limit) > 0:
                recipients = recipients[:int(limit)]
                break

        console.print("[bold white]first recipients:[/]")
        for recipient in recipients[:5]:
            console.print("  " + self.recipient_label(recipient))

        if not Confirm.ask(f"[bold red]send to {len(recipients)} recipients?"):
            return

        text = console.input("[bold red]text> [/]")

        delay = Prompt.ask(
            "[bold red]delay[/]",
            default="-".join(str(x) for x in self.settings.delay)
        )

        # CLI path sends plain text only; the bot supplies rich content (media/emoji/formatting).
        await self.run(recipients, RichContent(text=text), self.parse_delay(delay), console_report)

        self.print_stats()
