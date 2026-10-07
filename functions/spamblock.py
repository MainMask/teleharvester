import os
import re

from collections import Counter
from types import SimpleNamespace
from typing import Dict, List

from rich.prompt import Confirm

from telethon.errors import YouBlockedUserError
from telethon.sync import TelegramClient
from telethon.tl.functions.contacts import UnblockRequest

from functions.base import TelethonFunction
from functions.base.base import console_report
from modules import contacts_ledger, restricted_workers
from modules.storages.sessions_storage import release_client

# the report's groups, in order: working first, then the ones restricted, dead, unchecked
GROUPS = ("✅", "🚫", "⛔", "💀", "⚠️")


# @SpamBot answers in the session's language (random for an account added by phone): a temporary
# restriction is told by its release date's year, which every language writes the same
YEAR = re.compile(r"(?<!\d)20\d\d(?!\d)")
DATES = [re.compile(pattern) for pattern in (
    r"\d{1,2}\.?\s\S+\s20\d\d",                       # 12 Nov 2026, 12 нояб. 2026, 12. listopadu 2026
    r"20\d\d\s?年\s?\d{1,2}\s?月\s?\d{1,2}\s?日",       # 2026年11月12日
    r"\d{1,2}[./]\d{1,2}[./]20\d\d",                    # 12.11.2026
    r"20\d\d-\d\d-\d\d",                              # 2026-11-12
)]
REPLY_LIMIT = 700  # characters of a @SpamBot reply quoted in the report


def _group(line: str) -> int:
    return next((i for i, mark in enumerate(GROUPS) if line.startswith(mark)), len(GROUPS))


def worker_name(storage, path: str) -> str:
    """@username for a report, or the session file's name."""
    username = storage.usernames.get(path)
    return f"@{username}" if username else os.path.basename(path)


def release_date(text: str):
    """The release date in a @SpamBot reply (the year alone if its format is unknown); None if
    there is no year: a permanent restriction."""
    year = YEAR.search(text)
    if year is None:
        return None
    return next((m.group() for m in (d.search(text) for d in DATES) if m), year.group())


def _context():
    """What one status check reads once for all its workers (see SpamBlockFunc.scan)."""
    return SimpleNamespace(
        counts=Counter(contacts_ledger.load().values()),  # worker id -> its people
        released=set(restricted_workers.load_released()),
        replies={},  # id(session) -> (who, @SpamBot reply) of the permanently restricted
    )


class SpamBlockFunc(TelethonFunction):
    """Check accounts status"""

    _context = None  # set while scan() runs; check() alone builds its own

    def holds_people(self, path, counts, released) -> bool:
        """A worker some people wait for: its contacts (a .jsession knows its user id; a
        StringSession's people are everyone's anyway) not given to the other workers."""
        js = self.storage.jsessions_paths.get(path)
        return js is not None and path not in released and counts[js.account.account.user_id] > 0

    async def check(self, session: TelegramClient, report):
        context = self._context or _context()
        async with self.storage.ainitialize_session(session):
            path = self.storage.get_session_path(session)
            name = os.path.basename(path) if path else "?"
            try:
                me = await session.get_me()
            except Exception as err:
                self.progress_failed()
                await report(f"⚠️ {name} — не удалось опросить: {err}")
                return

            if me is None:  # get_me() swallows the auth error of a banned / logged-out account
                self.progress_failed()
                await report(f"💀 {name} — сессия мертва (бан или выход), перенесена в sessions/inactive")
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
                    self.progress_failed()
                    await report(f"⚠️ {who} — не удалось разблокировать @SpamBot: {err}")
                    return
                return await self.check(session, report)

            except Exception as err:
                self.progress_failed()
                await report(f"⚠️ {who} — не удалось проверить: {err}")
                return

            text = response.message
            lines = text.split("\n")

            if len(lines) == 1:
                self.progress_ok()
                await report(f"✅ {who} — без ограничений")
                return "active", session

            else:
                date = release_date(text)

                if date is None:
                    # excluded from mailing/contacts; its people wait for the admin's decision
                    # (the button after a status check), then go to other workers
                    context.replies[id(session)] = (who, text)
                    people = context.counts[me.id]
                    if not people:
                        tail = ""
                    elif self.holds_people(path, context.counts, context.released):
                        tail = f", его контакты ({people}) ждут решения"
                    else:
                        tail = f", его контакты ({people}) переданы другим воркерам"
                    await report(f"⛔ {who} — ограничен бессрочно{tail}")
                    return "permanent", session
                else:
                    await report(f"🚫 {who} — ЛС ограничены до {date}")
                    return date, session

    async def scan(self, report, replies: bool = False) -> Dict[str, List[TelegramClient]]:
        """Check every worker against @SpamBot; return {restriction_date: [sessions]}
        ("permanent" and "dead" are dates too). Rewrites the lists of permanently
        restricted workers and of the others' statuses: a worker that could not be
        checked keeps its entry; entries of sessions no longer on disk are dropped. The report
        comes at the end, grouped (see GROUPS), also when stopped midway; `replies` adds the
        @SpamBot reply of the permanently restricted ones (the admin's decision rests on it),
        once per distinct text, in worker order."""
        blocks: Dict[str, List[TelegramClient]] = {}

        lines = []

        async def collect(text):
            lines.append(text)

        context = self._context = _context()
        try:
            results = await self.gather_in_order(self.check, collect)
        finally:
            self._context = None
            for line in sorted(lines, key=_group):  # stable: worker order within a group
                await report(line)

        if replies:
            quotes = {}
            for session in self.sessions:  # worker order, not the order the checks finished in
                if id(session) in context.replies:
                    who, text = context.replies[id(session)]
                    quotes.setdefault(text, []).append(who)
            for text, whos in quotes.items():
                cut = text if len(text) <= REPLY_LIMIT else text[:REPLY_LIMIT] + "…"
                await report(f"💬 Ответ @SpamBot ({', '.join(whos)}):\n{cut}")

        excluded = {path for path in restricted_workers.load() if os.path.exists(path)}
        status = {path: s for path, s in restricted_workers.load_status().items() if os.path.exists(path)}
        released = {path for path in restricted_workers.load_released() if os.path.exists(path)}

        for result in results:
            if result is None:
                continue

            date, session = result
            path = self.storage.get_session_path(session)
            if path is None:  # already moved/forgotten
                pass
            elif date == "permanent":
                excluded.add(path)
                status.pop(path, None)
            else:
                excluded.discard(path)
                if date == "dead":
                    status.pop(path, None)
                else:  # "active" or a date
                    status[path] = date

            if date != "active":
                blocks.setdefault(date, []).append(session)

        restricted_workers.save(sorted(excluded))
        restricted_workers.save_status(status)
        # a worker no longer permanently restricted: should it be again, the admin decides again
        restricted_workers.save_released(sorted(released & excluded))
        return blocks

    def waiting(self) -> List[tuple]:
        """[(path, user_id, name, people)]: the permanently restricted workers in the pool whose
        contacts wait for the admin's decision."""
        context = _context()
        result = []
        for path in restricted_workers.load():
            if self.holds_people(path, context.counts, context.released):
                user_id = self.storage.jsessions_paths[path].account.account.user_id
                result.append((path, user_id, worker_name(self.storage, path), context.counts[user_id]))
        return result

    async def move_restricted(self, blocks: Dict[str, List[TelegramClient]]) -> List[str]:
        """Move restricted sessions into sessions/restricted/<date>/ (local filesystem op).
        One whose people wait for it (see holds_people) stays: out of sessions/ it would be
        gone, and its people handed to strangers. Returns the names of the ones kept."""
        if not os.path.exists("sessions/restricted"):
            os.mkdir("sessions/restricted")

        context = _context()
        kept = []
        for date, sessions in blocks.items():
            for session in sessions:
                path = os.path.join("sessions", "restricted", date.replace("/", "-"))  # 12/11/2026

                session_path = self.storage.get_session_path(session)
                if session_path is None:  # already moved/forgotten: nothing to relocate
                    continue
                if self.holds_people(session_path, context.counts, context.released):
                    kept.append(worker_name(self.storage, session_path))
                    continue

                if not os.path.exists(path):
                    os.mkdir(path)
                session_name = os.path.basename(session_path)

                await release_client(session)  # CLI clients stay connected: a forgotten one runs on
                os.rename(
                    session_path,
                    os.path.join(path, session_name)
                )
                self.storage._forget_session(session_path)  # no stale path for a later move

        return kept

    async def drop_dead(self, blocks: Dict[str, List[TelegramClient]]) -> List[TelegramClient]:
        """Take the dead sessions out of `blocks` and the pool (to sessions/inactive): their
        people become everyone's at once. Returns them."""
        dead = blocks.pop("dead", [])
        for session in dead:
            path = self.storage.get_session_path(session)
            if path is not None:
                await release_client(session)  # CLI clients stay connected: a forgotten one runs on
                self.storage.move_to_inactive(path)
        return dead

    async def run(self, report, move_restricted: bool = False,
                  replies: bool = False) -> Dict[str, List[TelegramClient]]:
        blocks = await self.scan(report, replies)
        await self.drop_dead(blocks)

        if move_restricted:
            await self.move_restricted(blocks)

        return blocks

    async def execute(self):
        self.ask_accounts_count()

        blocks = await self.run(console_report, replies=True)

        for path, _, name, people in self.waiting():
            if Confirm.ask(f"[bold magenta]Передать контакты {name} ({people}) другим воркерам? "
                           "Это не отменить.[/]", default=False):
                restricted_workers.release(path)

        if Confirm.ask("[bold magenta]Перенести ограниченные сессии в отдельные папки?[/]"):
            if kept := await self.move_restricted(blocks):
                await console_report(f"Оставлены в sessions/ — их контакты ждут: {', '.join(kept)}")

