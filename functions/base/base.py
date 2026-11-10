import asyncio
import random
import re
from urllib.parse import parse_qs, urlparse
from telethon import types
from telethon.errors import (
    FloodWaitError as RateLimitError,
    PeerFloodError as PeerLimitError,
    SlowModeWaitError,
    UserBannedInChannelError,
    UserDeactivatedBanError as AccountDeactivatedError,
    UserRestrictedError as AccountRestrictedError,
)

from modules.registry import menu_path
from modules import restricted_workers
from modules.account_limits import AccountLimited  # here too: the functions import it from this module


def pick_seconds(pause) -> int:
    """Seconds to wait for a [sec] or [min, max] setting (a reversed range, e.g. "5-2", tolerated)."""
    return pause[0] if len(pause) == 1 else random.randint(*sorted(pause[:2]))


POST_LINK_HINT = "Нужна ссылка на пост: t.me/name/123 или t.me/c/…/123."
_TME_LINK = re.compile(r"(?i)(?:https?://)?(?:www\.)?(?:t\.me|telegram\.(?:me|dog))/")  # a post's link starts so

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
    progress = None        # the bot's Progress for the running job (the 📊 button); None outside a job
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

    def progress_extend(self, n: int):  # n items get a second pass: n more steps in the total
        if self.progress is not None:
            self.progress.extend(n)

    # The job summary's counts: called next to the line reporting an item's or a worker's
    # outcome; totals, pauses and notes count nothing, whatever their wording.
    def progress_ok(self):
        if self.progress is not None:
            self.progress.ok += 1

    def progress_failed(self):
        if self.progress is not None:
            self.progress.failed += 1

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
        # the hosts _TME_LINK takes, in any case
        host = next((i for i, p in enumerate(parts) if p.lower().endswith(("t.me", "telegram.me", "telegram.dog"))),
                    None)
        return (parts[host + 1] if host is not None else parts[-2]), message_id

    @classmethod
    def is_message_link(cls, link: str) -> bool:
        """The link points at a post (see parse_message_link): checked as it is typed, not when
        every worker fails on it."""
        if not _TME_LINK.match(link.strip()):
            return False
        try:
            peer, _ = cls.parse_message_link(link)
        except (ValueError, IndexError):
            return False
        # t.me/c/<id> is a private chat's own link, t.me/+<hash> an invite: no post in either
        return not (isinstance(peer, str) and (peer == "c" or peer.startswith("+")))

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

    async def call(self, session, request):
        """session(request) through safe_call: short rate-limit waits waited out, an exhausted account flagged."""
        return await self.safe_call(lambda: session(request))

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

    async def run_sequential(self, work, report, sessions=None, pause="profile_pause"):
        """Run work(session, report) on each worker one at a time, pausing a random
        settings.<pause> ([min, max] or [sec]) between accounts (never before the first, nor
        after the last), read before each pause: one changed in ⚙️ applies to the running job.
        Returns the results in worker order.

        Firing the same action on every account within the same second (as gather_in_order
        does) marks the accounts as one operated cluster: profile-wide edits (name, bio,
        username, 2FA, photo) use the big profile_pause; engagement (reactions, votes) passes
        the shorter settings.delay. Either way, spacing the actions out breaks the pattern.
        """
        sessions = self.sessions if sessions is None else sessions
        self.progress_total(len(sessions))
        results = []

        for index, session in enumerate(sessions):
            if index:
                seconds = pick_seconds(getattr(self.settings, pause))
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
                         f"{menu_path('status')}")

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
          2. else, without a username, to the owner of their row: its access hash is valid for
             the account that scraped it only (a members base collected again by another
             account has rows of both, see scraper/members.py).
        Everyone else is a shared queue for all workers, the owners last: they take the shared
        people only once the others ran out (or when they are the only free workers). If a worker
        runs out (limit / restriction), its own people wait for it: nobody else takes them. A
        worker on hold keeps its people too: the caller holds its queue back.
        A plain list (.txt) has none of this: one queue for all workers; so has a list's line among a base's rows
        (📚 Вся база: modules/project_base.merged) — it is shared.
        """
        if not any(isinstance(row, dict) for row in rows):
            return [(self.sessions, rows)]

        by_id = {self.worker_id(s): s for s in self.sessions + self.on_hold}
        by_id.pop(None, None)
        owners = {id(by_id[row.get("owner_id")]) for row in rows
                  if isinstance(row, dict) and row.get("owner_id") in by_id}
        assigned = assigned or {}

        own, shared = {}, []
        for row in rows:
            worker = None
            if isinstance(row, dict):
                worker = by_id.get(assigned.get(str(row["user_id"])))
                if worker is None and not row.get("username"):
                    worker = by_id.get(row.get("owner_id"))
            if worker is None:
                shared.append(row)
            else:
                own.setdefault(id(worker), (worker, []))[1].append(row)

        queues = [([worker], worker_rows) for worker, worker_rows in own.values()]
        others = [s for s in self.sessions if id(s) not in owners]
        queues.append((others + [s for s in self.sessions if id(s) in owners], shared))
        return queues

    async def delay(self):
        await asyncio.sleep(pick_seconds(self.settings.delay))
