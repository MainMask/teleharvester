import asyncio
import random
from rich.console import Console
from rich.markup import escape
from rich.prompt import Prompt
from telethon import types
from telethon.errors import (
    FloodWaitError as RateLimitError,
    PeerFloodError as PeerLimitError,
    UserDeactivatedBanError as AccountDeactivatedError,
    UserRestrictedError as AccountRestrictedError,
)


_report_console = Console()


async def console_report(text: str) -> None:
    """Default progress reporter (CLI path): print plain text, no Rich markup parsing."""
    _report_console.print(text, markup=False, highlight=False)


class AccountLimited(Exception):
    """The account can no longer perform the action (rate limit / restriction / too long wait)."""


_END = object()  # sentinel for "no more items" (so a real None item isn't mistaken for the end)


class BaseFunction:
    rate_wait_limit = 300  # seconds; a longer wait means the account is exhausted
    max_rate_retries = 5   # consecutive rate-limit waits on one call before giving up
    delay_range = None     # per-run delay override; set by functions that prompt for a
                           # delay, so a choice doesn't leak into the shared Settings

    @staticmethod
    def safe(value) -> str:
        """Escape a value for safe use inside Rich markup (None -> '')."""
        return escape("" if value is None else str(value))

    def parse_delay(self, string: str):
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
        """
        parts = link.rstrip("/").split("/")
        message_id = int(parts[-1])

        if len(parts) >= 3 and parts[-3] == "c":
            return types.PeerChannel(int(parts[-2])), message_id

        return parts[-2], message_id

    async def safe_call(self, make_awaitable):
        """Run make_awaitable() (a no-arg callable returning a coroutine), waiting out
        short rate-limit waits and flagging inactive accounts via AccountLimited."""
        retries = 0

        while True:
            try:
                return await make_awaitable()
            except RateLimitError as err:
                if err.seconds <= self.rate_wait_limit and retries < self.max_rate_retries:
                    retries += 1
                    await asyncio.sleep(err.seconds + 1)
                    continue

                raise AccountLimited(f"rate limit {err.seconds}s")
            except (PeerLimitError, AccountDeactivatedError, AccountRestrictedError) as err:
                raise AccountLimited(str(err))

    async def run_with_rotation(self, items, action):
        """Process items with one account, rotating to the next when it gets limited.

        `action(session, item)` does the per-item work (via `safe_call`); on
        `AccountLimited` the same item is retried on the next account.

        Returns the number of items processed; a caller with a known total can tell
        when the accounts ran out before the list did.
        """
        items = iter(items)
        item = next(items, _END)
        processed = 0

        for session in self.sessions:
            if item is _END:
                break

            async with self.storage.ainitialize_session(session):
                while item is not _END:
                    try:
                        await action(session, item)
                    except AccountLimited:
                        break

                    processed += 1
                    item = next(items, _END)
                    if item is not _END:  # no trailing delay after the final item
                        await self.delay()

        return processed

    def ask_accounts_count(self):
        self.sessions = self.storage.sessions  # reset to full list (instance is reused across runs)

        if not self.sessions:
            return  # nothing to choose from; functions guard the empty case themselves

        accounts_count = self.ask_int(
            "[bold magenta]how many accounts to use? [/]",
            default=len(self.sessions),
            min_value=1,
        )

        self.sessions = self.sessions[:accounts_count]

    async def delay(self):
        delay = self.delay_range or self.settings.delay
        if len(delay) == 1:
            await asyncio.sleep(delay[0])
        else:
            await asyncio.sleep(
                random.randint(*delay)
            )
