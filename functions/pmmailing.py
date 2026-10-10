import asyncio
import os
from datetime import datetime

from telethon import types
from telethon.errors import PeerIdInvalidError
from telethon.tl.functions.users import GetRequirementsToContactRequest
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

# Recipients asked about in one users.getRequirementsToContact call (Telegram documents no
# limit; a failed call leaves its batch unchecked (reported once), see check_requirements).
REQUIREMENTS_BATCH = 100


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
                raise ValueError(f"номер {recipient} не найден")
            return users[0]

        return recipient

    async def account(self, session):
        if id(session) not in self._me_cache:
            try:
                me = await session.get_me()
            except Exception as err:
                raise AccountLimited(f"не удалось опросить аккаунт: {err}")

            if me is None:
                raise AccountLimited("аккаунт не авторизован")

            self._me_cache[id(session)] = me

        return self._me_cache[id(session)]

    async def check_requirements(self, session, recipient, me):
        """Ask Telegram what this worker needs to write to `recipient` and the base's
        next people in its queue (one request for up to REQUIREMENTS_BATCH): the answer
        depends on the worker (its Premium, its contacts), so it is cached per worker.
        A failed check leaves the batch unknown: they are sent to as without it."""
        rows, start = self._positions.get(id(recipient), ([recipient], 0))
        batch = []
        for row in rows[start:]:
            if len(batch) == REQUIREMENTS_BATCH:
                break
            if row.get("owner_id") in (None, me.id) and (id(session), row["user_id"]) not in self._requirements:
                batch.append(row)

        request = GetRequirementsToContactRequest(
            [types.InputUser(row["user_id"], row["access_hash"]) for row in batch]
        )
        try:
            result = await self.safe_call(lambda: session(request))
            if len(result) != len(batch):
                raise ValueError(f"{len(result)} ответов на {len(batch)} получателей")
        except AccountLimited:
            raise
        except Exception as err:
            result = [None] * len(batch)
            if not self._check_failed:
                self._check_failed = True
                await self._report(f"проверка настроек получателей не удалась, отправляю без неё: {err}")

        for row, requirement in zip(batch, result):
            self._requirements[(id(session), row["user_id"])] = requirement

    async def contact_requirement(self, session, recipient, me):
        """Why the recipient's settings refuse this worker (see check_requirements), or None."""
        key = (id(session), recipient["user_id"])
        if key not in self._requirements:
            await self.check_requirements(session, recipient, me)

        requirement = self._requirements[key]
        if isinstance(requirement, types.RequirementToContactPaidMessages):
            return rich_message.paid_reason(requirement.stars_amount)
        if isinstance(requirement, types.RequirementToContactPremium) and not me.premium:
            return rich_message.PREMIUM_ONLY
        return None

    def hand_to_premium(self, recipient, me) -> bool:
        """Keep a "Premium only" refusal for the run's Premium workers (see premium_pass)
        when another worker may take this recipient at all: one of the shared queue."""
        if self._premium_pass or me.premium:
            return False
        if not (isinstance(recipient, str) or id(recipient) in self._shared_ids):
            return False  # the base owner's only, or a worker's own contact
        if not self.premium_candidates():
            return False
        self._for_premium.append(recipient)
        return True

    def premium_candidates(self):
        """The shared queue's workers that may have Premium: not out this run, and not
        polled without it (one not polled yet is asked in the pass)."""
        return [s for s in self._shared_sessions if id(s) not in self._out
                and (id(s) not in self._me_cache or self._me_cache[id(s)].premium)]

    async def send_tracked(self, session, recipient):
        """send_one, remembering the workers that ran out this run (no Premium pass for them)."""
        try:
            return await self.send_one(session, recipient)
        except AccountLimited:
            self._out.add(id(session))
            raise

    async def send_premium(self, session, recipient):
        """send_one for the Premium pass: a worker without Premium passes the recipient on."""
        if not (await self.account(session)).premium:
            raise AccountLimited("нет Premium")
        return await self.send_one(session, recipient)

    async def premium_pass(self, report):
        """The shared queue's "Premium only" refusals, again, by the Premium workers still free."""
        recipients, self._premium_pass = self._for_premium, True
        candidates = self.premium_candidates()  # one polled without Premium isn't even connected
        done = 0
        if candidates:
            await report(f"Ищу Premium-воркера для {len(recipients)} получателей «только Premium»")
            self.progress_extend(len(recipients))  # their first pass is already counted
            # a requirements check now takes the next of these, not the rest of their first queue
            self._positions.update({id(row): (recipients, i) for i, row in enumerate(recipients)
                                    if isinstance(row, dict)})
            done = await self.run_with_rotation(recipients, self.send_premium, candidates)
        if left := len(recipients) - done:
            self._premium_only += left
            if candidates:
                self.progress_drop(left)
            await report(f"Нет свободного Premium-воркера: {left} получателей «только Premium» не отправлены")

    async def pause_between_accounts(self, account_key, name):
        if account_key in self._active_accounts:
            return

        if self._active_accounts:  # not the first account to send
            seconds = pick_seconds(self.settings.account_pause)

            await self._report(f"переход на {name}, пауза {seconds} с")
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
            raise AccountLimited(f"дневной лимит {self.settings.per_account_daily} исчерпан")

        await self.pause_between_accounts(account_key, name)

        # a base row with this worker's own hash: checkable before any send (a username or a
        # phone would cost a resolve each; those learn it from the send's error instead)
        if isinstance(recipient, dict) and not foreign:
            reason = await self.contact_requirement(session, recipient, me)
            if reason:
                if reason != rich_message.PREMIUM_ONLY:
                    self._paid += 1
                elif self.hand_to_premium(recipient, me):
                    reason += ", передаю Premium-воркеру"
                else:
                    self._premium_only += 1
                await self._report(f"[{name}] пропущено: {self.recipient_label(recipient)} — {reason}")
                return SKIPPED

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
            reason = rich_message.refusal_reason(err)
            if reason == rich_message.PREMIUM_ONLY and self.hand_to_premium(recipient, me):
                await self._report(f"[{name}] не отправлено: {self.recipient_label(recipient)} "
                                   f"— {reason}, передаю Premium-воркеру")
                return
            self.progress_failed()
            await self._report(f"[{name}] не отправлено: {self.recipient_label(recipient)} "
                               + (f"— {reason}" if reason else str(err)))
            return

        self.record_success(key)
        self._limits.bump(account_key)
        self.progress_ok()
        await self._report(
            "[{name}] отправлено{via}: {recipient}, всего: {count}".format(
                name=name,
                via=" (по username)" if fallback else "",
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
        self._requirements = {}  # (id(session), user_id) -> RequirementToContact*, None: unknown
        self._check_failed = False
        self._premium_only = self._paid = 0
        self._for_premium = []  # "Premium only" refusals of the shared queue, see premium_pass
        self._premium_pass = False
        self._out = set()  # id(session) of the workers that ran out this run
        self._shared_sessions = []
        self.delay_range = delay
        self.progress_prepare()

        self.load_stats()
        self._limits = DailyCounter(LIMITS_PATH, self.settings.per_account_daily)  # 0: unlimited
        await self.check_workers(report)
        self.progress_total(len(recipients))

        try:
            processed = waiting = 0
            queues = self.split_queues(recipients, contacts_ledger.load())
            # where each base row stands in its queue: a requirements check takes the people after it
            self._positions = {id(row): (rows, i) for _, rows in queues
                               for i, row in enumerate(rows) if isinstance(row, dict)}
            *own, (shared_sessions, shared) = queues
            self._shared_ids = {id(row) for row in shared if isinstance(row, dict)}
            for (worker,), queue in own:
                if worker in self.on_hold:  # on hold (scraping, or left out of a CLI run): its people wait, as for a daily cap
                    waiting += len(queue)
                    await report(f"{len(queue)} получателей ждут своего воркера: он не участвует в этом запуске.")
                    self.progress_drop(len(queue))
                    continue
                done = await self.run_with_rotation(queue, self.send_tracked, [worker])
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
                    self._shared_ids.update(id(row) for row in rest)
                    await report(f"Воркер выбыл: {len(rest)} его получателей переданы другим воркерам.")
                else:
                    waiting += len(rest)
                    await report(f"{len(rest)} получателей ждут своего воркера до следующего запуска.")
                    self.progress_drop(len(rest))  # not this run: out of the total
            self._shared_sessions = shared_sessions
            processed += await self.run_with_rotation(shared, self.send_tracked, shared_sessions)
            if self._for_premium:
                await self.premium_pass(report)
        finally:
            self.flush_stats()  # persist the tail on any exit (success, error, cancel)

        if self._skipped:
            await report(f"Пропущено без username (база другого аккаунта): {self._skipped}")

        if self._premium_only or self._paid:
            await report(f"Пропущено по настройкам получателя: только Premium — {self._premium_only}, "
                         f"платные — {self._paid}")

        if processed + waiting < len(recipients):
            await report(
                f"Внимание: обработано {processed}/{len(recipients)} — аккаунты исчерпаны, "
                "остаток не отправлен."
            )
        else:
            await report(f"Итого обработано получателей: {processed}")

    def print_stats(self):
        table = Table()

        table.add_column("Получатель", justify="left", style="white")
        table.add_column("Кол-во", justify="center", style="white")
        table.add_column("Последняя отправка", style="white")

        for recipient, entry in self.stats.items():
            table.add_row(
                self.safe(recipient), str(entry["count"]), entry.get("last_date", "—")
            )

        console.print(table)

    async def execute(self):
        self.ask_accounts_count()

        path = self.ask_file(
            "[bold red]файл с получателями[/]",
            scraped_files.participant_bases(),  # the scraped bases, newest first, as in the bot
            default=os.path.join("assets", "targets.txt"),
        )

        if not os.path.exists(path):
            console.print("[bold red]Файл не найден!")
            return

        try:
            recipients = self.load_recipients(path)
        except Exception as err:
            console.print(f"[bold red]{err}")
            return

        if not recipients:
            console.print("[bold red]Список получателей пуст!")
            return

        self.load_stats()

        if Confirm.ask("[bold red]пропускать тех, кому уже писали?", default=True):
            before = len(recipients)
            recipients = self.filter_unsent(recipients)
            console.print(f"[bold white]пропущено уже получивших: {before - len(recipients)}[/]")

            if not recipients:
                console.print("[bold red]Отправлять некому!")
                return

        while True:
            limit = Prompt.ask(
                "[bold red]сколько получателей (пусто — все)[/]",
                default=""
            )

            if not limit:
                break

            if limit.isdigit() and int(limit) > 0:
                recipients = recipients[:int(limit)]
                break

        console.print("[bold white]первые получатели:[/]")
        for recipient in recipients[:5]:
            console.print("  " + self.safe(self.recipient_label(recipient)))

        if not Confirm.ask(f"[bold red]отправить {len(recipients)} получателям?"):
            return

        text = console.input("[bold red]текст> [/]")

        delay = Prompt.ask(
            "[bold red]задержка[/]",
            default="-".join(str(x) for x in self.settings.delay)
        )

        # CLI path sends plain text only; the bot supplies rich content (media/emoji/formatting).
        await self.run(recipients, RichContent(text=text), self.parse_delay(delay), console_report)

        self.print_stats()
