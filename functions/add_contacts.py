import asyncio

from telethon.tl.functions.contacts import AddContactRequest
from telethon.tl.types import InputUser
from rich.prompt import Prompt

from functions.base import TelethonFunction
from functions.base.base import SKIPPED, AccountLimited, console_report
from modules import contacts_ledger, parquet_db, scraped_files
from modules.account_limits import DailyCounter


LEDGER_SAVE_EVERY = 25  # buffered like the mailing stats; run() always flushes the tail


class AddContactsFunc(TelethonFunction):
    """Add users to contacts from a .parquet database"""

    async def _add(self, session, user, row):
        await self.safe_call(lambda: session(AddContactRequest(
            id=user,
            # a scrape base has the full name in "name" (no first/last split)
            first_name=row.get("first_name") or row.get("name") or row.get("username") or "contact",
            last_name=row.get("last_name") or "",
            phone=row.get("phone") or ""
        )))

    async def my_id(self, session):
        if id(session) not in self._my_ids:
            try:
                self._my_ids[id(session)] = (await session.get_me()).id
            except Exception as err:
                raise AccountLimited(f"не удалось опросить аккаунт: {err}")
        return self._my_ids[id(session)]

    async def _add_by_username(self, session, row):
        user = await self.safe_call(lambda: session.get_input_entity(row["username"]))
        await self._add(session, user, row)

    async def add_one(self, session, row):
        aid = await self.my_id(session)

        if self._limits.reached(aid):
            # out for today: rotation moves this person to the next account (item not lost)
            raise AccountLimited(f"дневной лимит {self._limits.cap} исчерпан")

        owner = row.get("owner_id")
        foreign = owner is not None and owner != aid

        if foreign and not row.get("username"):
            self.skipped += 1  # only the base's owner can reach them
            return SKIPPED

        try:
            if foreign:  # the base's access hash belongs to another account: it can't work here
                await self._add_by_username(session, row)
            else:
                try:
                    await self._add(session, InputUser(row["user_id"], row["access_hash"]), row)
                except AccountLimited:
                    raise
                except Exception:
                    # access_hash is per account: on any worker but the one that scraped the
                    # database it is invalid, so fall back to the username when there is one
                    if not row.get("username"):
                        raise
                    await self._add_by_username(session, row)
        except AccountLimited:
            raise
        except Exception as err:
            self.progress_failed()
            await self._report(f"пропуск user_id={row.get('user_id')}: {err}")
            return

        self.added += 1
        self._limits.bump(aid)
        self.ledger[str(row["user_id"])] = aid  # the mailing routes them here
        self._unsaved += 1
        if self._unsaved >= LEDGER_SAVE_EVERY:
            self.flush_ledger()
        self.progress_ok()
        await self._report(f"добавлен user_id={row['user_id']}, всего: {self.added}")

    def flush_ledger(self):
        if self._unsaved:
            contacts_ledger.save(self.ledger)
            self._unsaved = 0

    async def run(self, path, delay, report):
        self._report = report
        self.delay_range = delay
        self.added = 0
        self.skipped = 0
        self._my_ids = {}
        self._unsaved = 0
        self._limits = DailyCounter(
            "stats/contacts_limits.json", self.settings.contacts_per_account_daily
        )
        self.progress_prepare()

        try:
            # off the event loop: pq.read_table + to_pylist is blocking and a large
            # DB would otherwise stall the bot's polling loop for its whole duration
            rows = await asyncio.to_thread(parquet_db.load, path)
        except Exception as err:
            self.progress_failed()
            await report(str(err))
            return

        if not rows:
            self.progress_failed()
            await report("База пуста!")
            return

        self.ledger = contacts_ledger.load()
        await self.check_workers(report)
        # already in a current worker's contacts (one on hold too): skip; a removed worker's
        # people are re-added
        workers = {self.worker_id(s) for s in self.sessions + self.on_hold} - {None}
        total = len(rows)
        rows = [r for r in rows if self.ledger.get(str(r["user_id"])) not in workers]
        already = total - len(rows)
        self.progress_total(len(rows))

        processed = waiting = 0
        stopped = set()  # workers that ran out on a queue: not tried again this run
        try:
            for sessions, queue in self.split_queues(rows):
                if sessions and sessions[0] in self.on_hold:  # on hold (scraping, or left out of a CLI run): its people wait for it
                    waiting += len(queue)
                    self.progress_drop(len(queue))
                    continue
                sessions = [s for s in sessions if id(s) not in stopped]
                done = await self.run_with_rotation(queue, self.add_one, sessions)
                if done < len(queue):
                    stopped.update(id(s) for s in sessions)
                processed += done
                self.progress_drop(len(queue) - done)  # its accounts ran out: the rest is skipped
        finally:
            self.flush_ledger()  # persist the tail on any exit (success, error, cancel)
            self.ledger = {}  # every run reloads it: the shared instance must not hold it between runs

        await report(f"Готово. Добавлено контактов: {self.added}")
        if already:
            await report(f"Уже в контактах (пропущено): {already}")
        if self.skipped:
            await report(f"Пропущено без username (база другого аккаунта): {self.skipped}")
        if waiting:
            await report(f"Ждут своего воркера (он не участвует в этом запуске): {waiting}")
        if processed + waiting < len(rows):
            await report(
                f"Внимание: обработано {processed}/{len(rows)} — аккаунты исчерпаны, "
                "остаток пропущен."
            )

    async def execute(self):
        self.ask_accounts_count()

        path = self.ask_file(
            "[bold red]путь к .parquet[/]",
            scraped_files.participant_bases(),  # the scraped bases, newest first, as in the bot
            default="assets/contacts.parquet",
        )

        delay = Prompt.ask(
            "[bold red]задержка[/]",
            default="-".join(str(x) for x in self.settings.delay)
        )

        await self.run(path, self.parse_delay(delay), console_report)
