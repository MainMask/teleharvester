import asyncio
import random
from urllib.parse import parse_qs, urlparse
from rich.console import Console
from rich.markup import escape
from rich.prompt import Prompt
from telethon import types
from telethon.errors import (
    FloodWaitError as RateLimitError,
    PeerFloodError as PeerLimitError,
    SlowModeWaitError,
    UserBannedInChannelError,
    UserDeactivatedBanError as AccountDeactivatedError,
    UserRestrictedError as AccountRestrictedError,
)

from modules import restricted_workers


_report_console = Console()


async def console_report(text: str) -> None:
    """Default progress reporter (CLI path): print plain text, no Rich markup parsing."""
    _report_console.print(text, markup=False, highlight=False)


class AccountLimited(Exception):
    """The account can no longer perform the action (rate limit / restriction / too long wait)."""


def pick_seconds(pause) -> int:
    """Seconds to wait for a [sec] or [min, max] setting (a reversed range, e.g. "5-2", tolerated)."""
    return pause[0] if len(pause) == 1 else random.randint(*sorted(pause[:2]))


_END = object()  # sentinel for "no more items" (so a real None item isn't mistaken for the end)
SKIPPED = object()  # returned by a rotation action that skipped an item without any request


def _problems_only(report):
    """report for a @SpamBot check inside a run: "✅ active" × N would bury the run's own report."""
    async def problems(text):
        if not text.startswith("✅"):
            await report(text)
    return problems


class BaseFunction:
    rate_wait_limit = 300  # seconds; a longer wait means the account is exhausted
    max_rate_retries = 5   # consecutive rate-limit waits on one call before giving up
    delay_range = None     # per-run delay override; set by functions that prompt for a
                           # delay, so a choice doesn't leak into the shared Settings
    progress = None        # the bot's Progress for the running job (the 📊 button); None on the CLI
    on_hold = []           # workers the bot keeps out of this run for now (the one the scraper runs
                           # on, see WorkerPool.delegate): not gone, their people wait for them

    def progress_prepare(self):  # the total comes after some prep (a check, a parse)
        if self.progress is not None:
            self.progress.prepare()

    def progress_total(self, total: int | None):  # None: open-ended, count steps only
        if self.progress is not None:
            self.progress.start(total)

    def progress_step(self):
        if self.progress is not None:
            self.progress.step()

    def progress_drop(self, n: int):  # a worker stopped early: n of the total won't happen
        if self.progress is not None:
            self.progress.drop(n)

    # The job summary's counts: called next to the line reporting an item's or a worker's
    # outcome; totals, pauses and notes count nothing, whatever their wording.
    def progress_ok(self):
        if self.progress is not None:
            self.progress.ok += 1

    def progress_failed(self):
        if self.progress is not None:
            self.progress.failed += 1

    @staticmethod
    def safe(value) -> str:
        """Escape a value for safe use inside Rich markup (None -> '')."""
        return escape("" if value is None else str(value))

    @staticmethod
    def parse_delay(string: str):
        return list(
            map(int, string.split("-"))
        )

    @staticmethod
    def ask_int(label: str, default=None, min_value: int | None = None) -> int:
        """Prompt for an integer, re-asking until the input is valid."""
        while True:
            raw = Prompt.ask(label, default=None if default is None else str(default))
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if min_value is not None and value < min_value:
                continue
            return value

    @staticmethod
    def parse_message_link(link):
        """Parse a t.me message link into (peer, message_id).

        Public: t.me/<name>/<id> -> ("<name>", id).
        Private: t.me/c/<channel_id>/<id> -> (PeerChannel(channel_id), id).
        Topic links (.../<topic>/<id>) and a ?single / ?comment= query are accepted.
        """
        parts = link.split("?")[0].rstrip("/").split("/")
        message_id = int(parts[-1])

        if "c" in parts[:-2]:
            return types.PeerChannel(int(parts[parts.index("c") + 1])), message_id

        # the peer is the segment right after the host (a topic id may follow it)
        host = next((i for i, p in enumerate(parts) if p.endswith(("t.me", "telegram.me"))), None)
        return (parts[host + 1] if host is not None else parts[-2]), message_id

    @staticmethod
    def comment_id(link) -> int | None:
        """The comment a t.me post link points at (…/<post>?comment=<id>), or None."""
        comment = parse_qs(urlparse(link.strip()).query).get("comment")
        return int(comment[0]) if comment and comment[0].isdigit() else None

    async def safe_call(self, make_awaitable):
        """Run make_awaitable() (a no-arg callable returning a coroutine), waiting out
        short rate-limit waits and flagging inactive accounts via AccountLimited."""
        retries = 0

        while True:
            try:
                return await make_awaitable()
            except (RateLimitError, SlowModeWaitError) as err:
                if err.seconds <= self.rate_wait_limit and retries < self.max_rate_retries:
                    retries += 1
                    await asyncio.sleep(err.seconds + 1)
                    continue

                raise AccountLimited(f"флуд-лимит {err.seconds} с")
            # UserBannedInChannel: the account is spam-restricted from all groups/channels
            except (PeerLimitError, AccountDeactivatedError, AccountRestrictedError,
                    UserBannedInChannelError) as err:
                raise AccountLimited(str(err))

    async def run_with_rotation(self, items, action, sessions=None):
        """Process items with one account, rotating to the next when it gets limited.

        `action(session, item)` does the per-item work (via `safe_call`); on
        `AccountLimited` the same item is retried on the next account. `sessions`
        defaults to all of self.sessions.

        Returns the number of items processed; a caller with a known total can tell
        when the accounts ran out before the list did.
        """
        items = iter(items)
        item = next(items, _END)
        processed = 0

        for session in self.sessions if sessions is None else sessions:
            if item is _END:
                break

            async with self.storage.ainitialize_session(session):
                while item is not _END:
                    try:
                        result = await action(session, item)
                    except AccountLimited:
                        break

                    processed += 1
                    self.progress_step()
                    item = next(items, _END)
                    # no trailing delay after the final item, nor after one skipped without a request
                    if item is not _END and result is not SKIPPED:
                        await self.delay()

        return processed

    async def gather_in_order(self, work, report, sessions=None):
        """Run work(session, report) on every worker at once, but keep the report in
        worker order: the first unfinished worker reports live, later ones are held
        until every worker before them is done. Returns the results in worker order.
        Meant for one-shot jobs: a long one would hide all workers but the first."""
        sessions = self.sessions if sessions is None else sessions
        held = [[] for _ in sessions]
        done = [False] * len(sessions)
        current = 0
        flushing = False  # one flusher at a time, or two could interleave held lines
        self.progress_total(len(sessions))

        def reporter(index):
            async def worker_report(text):
                # while a flusher is sending this worker's held lines, a new one queues behind them
                if index == current and not held[index] and not flushing:
                    await report(text)
                else:
                    held[index].append(text)
            return worker_report

        async def one(index, session):
            nonlocal current, flushing
            try:
                return await work(session, reporter(index))
            finally:
                done[index] = True
                self.progress_step()
                if not flushing:  # else the running flusher sees this worker done
                    flushing = True
                    try:
                        while current < len(sessions):
                            while held[current]:  # lines reported meanwhile land here too
                                await report(held[current].pop(0))
                            if not done[current]:
                                break
                            current += 1
                    finally:
                        flushing = False

        return await asyncio.gather(*[one(i, s) for i, s in enumerate(sessions)])

    async def run_sequential(self, work, report, sessions=None, pause=None):
        """Run work(session, report) on each worker one at a time, pausing a random
        `pause` ([min, max] or [sec], default settings.profile_pause) between accounts
        (never before the first, nor after the last). Returns the results in worker order.

        Firing the same action on every account within the same second (as gather_in_order
        does) marks the accounts as one operated cluster: profile-wide edits (name, bio,
        username, 2FA, photo) use the big profile_pause; engagement (reactions, votes) passes
        the shorter settings.delay. Either way, spacing the actions out breaks the pattern.
        """
        sessions = self.sessions if sessions is None else sessions
        pause = self.settings.profile_pause if pause is None else pause
        self.progress_total(len(sessions))
        results = []

        for index, session in enumerate(sessions):
            if index:
                seconds = pick_seconds(pause)
                await report(f"Пауза перед следующим аккаунтом: {seconds} с")
                await asyncio.sleep(seconds)
            results.append(await work(session, report))
            self.progress_step()

        return results

    def worker_id(self, session):
        """The worker's Telegram user id from its .jsession (no request); None if unknown."""
        js = self.storage.jsessions_paths.get(self.storage.get_session_path(session))
        return js.account.account.user_id if js is not None else None

    def drop_restricted(self) -> int:
        """Leave out of self.sessions the workers the last status check found permanently
        restricted; returns how many were left out. Until the admin releases its contacts
        (stats/released.json) one goes on hold: its people wait. A released one is out for
        good, on hold too: its people go to the other workers."""
        excluded = set(restricted_workers.load())
        if not excluded:
            return 0
        released = set(restricted_workers.load_released())
        path = self.storage.get_session_path
        out = [s for s in self.sessions if path(s) in excluded]
        gone = [s for s in self.on_hold if path(s) in excluded and path(s) in released]
        self.sessions = [s for s in self.sessions if path(s) not in excluded]
        self.on_hold = [s for s in self.on_hold if s not in gone] + [s for s in out if path(s) not in released]
        return len(out) + len(gone)

    async def check_workers(self, report):
        """Ask @SpamBot about self.sessions before a run: dead sessions leave the pool,
        permanently restricted ones are left out (see functions/spamblock.py), ones
        restricted until a date sit this run out on hold."""
        from functions.spamblock import SpamBlockFunc  # spamblock imports this module

        await report("Проверяю воркеров у @SpamBot…")
        checker = SpamBlockFunc(self.storage, self.settings)
        checker.sessions = self.sessions

        blocks = await checker.run(_problems_only(report))
        # a dead session was moved to sessions/inactive and forgotten by the storage
        self.sessions = [s for s in self.sessions if self.storage.get_session_path(s) is not None]

        # restricted until a date: can't write to strangers, so not this run; its people wait for it
        until = [s for date, sessions in blocks.items() if date != "permanent" for s in sessions]
        if until:
            self.sessions = [s for s in self.sessions if s not in until]
            self.on_hold = self.on_hold + until  # a new list: on_hold defaults to a class attribute
            await report(f"Пропущено воркеров, ограниченных до даты: {len(until)}")

        if dropped := self.drop_restricted():
            await report(f"Пропущено бессрочно ограниченных воркеров: {dropped}")
        if waiting := checker.waiting():
            await report(f"Контакты бессрочно ограниченных воркеров ждут решения "
                         f"({sum(people for *_, people in waiting)} чел.): "
                         "🤖 Воркеры → 🩺 Проверка и статистика → Проверка статуса")

    async def worker_gone(self, session, report) -> bool:
        """Ask @SpamBot about a worker that stopped mid-run: True if it is out for good, i.e.
        dead (moved to sessions/inactive). A permanently restricted one is not: its people wait
        for the admin's decision (see drop_restricted)."""
        from functions.spamblock import SpamBlockFunc  # spamblock imports this module

        checker = SpamBlockFunc(self.storage, self.settings)
        checker.sessions = [session]

        blocks = await checker.scan(_problems_only(report))
        return bool(await checker.drop_dead(blocks))

    def split_queues(self, rows, assigned=None):
        """[(sessions, rows), ...]: which workers may take which people of a scraped base.

        A person goes to one worker when
          1. `assigned` ({"<user_id>": worker id}, the contacts ledger) names a current worker;
          2. else, without a username, to the base's owner: its access hashes are valid for
             the account that scraped it only.
        Everyone else is a shared queue for all workers, the owner last: it takes the shared
        people only once the others ran out (or when it is the only free worker). If a worker
        runs out (limit / restriction), its own people wait for it: nobody else takes them. A
        worker on hold keeps its people too: the caller holds its queue back.
        A plain list (.txt) has none of this: one queue for all workers.
        """
        if not rows or not isinstance(rows[0], dict):
            return [(self.sessions, rows)]

        by_id = {self.worker_id(s): s for s in self.sessions + self.on_hold}
        by_id.pop(None, None)
        owner = by_id.get(rows[0].get("owner_id"))
        assigned = assigned or {}

        own, shared = {}, []
        for row in rows:
            worker = by_id.get(assigned.get(str(row["user_id"])))
            if worker is None and owner is not None and not row.get("username"):
                worker = owner
            if worker is None:
                shared.append(row)
            else:
                own.setdefault(id(worker), (worker, []))[1].append(row)

        queues = [([worker], worker_rows) for worker, worker_rows in own.values()]
        others = [s for s in self.sessions if s is not owner]
        queues.append((others + [s for s in self.sessions if s is owner], shared))
        return queues

    @staticmethod
    def ask_file(label: str, files: list, default: str = "") -> str:
        """Prompt for a file: a number from `files` ([(path, caption)], see
        modules.scraped_files) or a typed path; blank takes `default`."""
        for index, (_, caption) in enumerate(files, 1):
            _report_console.print(f"  [{index}] {caption}", markup=False, highlight=False)
        if files:
            _report_console.print("[bold white]введите номер или путь к файлу[/]")

        while True:
            raw = (Prompt.ask(label, default=default or None) or "").strip() or default
            if raw.isdigit() and 1 <= int(raw) <= len(files):
                return files[int(raw) - 1][0]
            if raw:
                return raw

    def ask_accounts_count(self):
        self.sessions = self.storage.sessions  # reset to full list (instance is reused across runs)
        self.on_hold = []

        if not self.sessions:
            return  # nothing to choose from; functions guard the empty case themselves

        accounts_count = self.ask_int(
            "[bold magenta]сколько аккаунтов использовать? [/]",
            default=len(self.sessions),
            min_value=1,
        )

        # the ones left out keep their contacts' people (see split_queues), as a worker on hold
        self.sessions, self.on_hold = self.sessions[:accounts_count], self.sessions[accounts_count:]

    async def delay(self):
        await asyncio.sleep(pick_seconds(self.delay_range or self.settings.delay))
