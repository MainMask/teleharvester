import asyncio
import random


from functions.base import TelethonFunction
from functions.base.base import AccountLimited
from modules import profile_done, rich_message
from modules.account_limits import DailyCounter

COMMENTS_LIMITS = "stats/comments_limits.json"  # the comments a worker wrote today: 🗨 and 🆕 count together


def comments_cap(settings) -> DailyCounter:
    """[limits] comments_per_account_daily, kept in COMMENTS_LIMITS (0: no limit)."""
    return DailyCounter(COMMENTS_LIMITS, settings.comments_per_account_daily)


def worker_key(storage, session, limits: DailyCounter) -> str | None:
    """The worker in COMMENTS_LIMITS: its Telegram id, or its file's path if that is unknown; None with no cap set
    (`limits` then reads and writes nothing)."""
    if not limits.cap:
        return None
    path = storage.get_session_path(session)
    return profile_done.worker_key(storage, path) or path


def variants(contents: list):
    """The contents to send, one after another: each round in a random order, so none comes twice before every one
    has (the lines of a .txt of variants); a single message every time."""
    while True:
        yield from random.sample(contents, len(contents))


class CommentsBroadcastFunc(TelethonFunction):
    """Broadcast to channel comments"""

    def __init__(self, storage, settings):
        super().__init__(storage, settings)
        # the run's own, as it was started: config.toml's is every project's, another project's run may change it
        self.messages_count = settings.messages_count
        self._limits = comments_cap(settings)

    async def broadcast(self, session, channel, post_id, texts, report):
        """`texts`: the contents to send, one a comment (variants) — the post's, shared by its workers."""
        async with self.storage.ainitialize_session(session):
            me = await self.poll_me(session, report)
            if me is None:
                self.progress_drop(self.messages_count)
                return

            try:
                channel = await self.resolve_chat(session, channel)
            except Exception as err:
                self.progress_failed()
                await report(f"[{me.first_name}] канал не найден: {err}")
                self.progress_drop(self.messages_count)
                return

            count = 0
            errors = 0
            key = worker_key(self.storage, session, self._limits)

            while count < self.messages_count \
                    or self.messages_count == 0:
                if self._limits.reached(key):
                    await report(f"[{me.first_name}] дневной лимит комментариев достигнут, стоп")
                    break
                try:
                    await rich_message.send(
                        session, channel, next(texts), self.safe_call,
                        report=report, comment_to=post_id,
                    )
                except AccountLimited as err:
                    self.progress_failed()
                    await report(f"[{me.first_name}] лимит, остановка. {err}")
                    break
                except Exception as err:
                    self.progress_failed()
                    await report(f"[{me.first_name}] не отправлено: {err}")

                    errors += 1

                    if errors >= 5:
                        await report(f"[{me.first_name}] 5 ошибок подряд, остановка.")
                        break
                else:
                    self._limits.bump(key)
                    errors = 0
                    count += 1
                    self.progress_step()
                    self.progress_ok()
                    await report(f"[{me.first_name}] отправлено, всего: {count}")

                # delay between sends only; a break (limit / 5 errors) skips it, as does the last send
                if not (self.messages_count and count >= self.messages_count):
                    await self.delay()

            if self.messages_count:  # stopped early: its unsent rest leaves the total
                self.progress_drop(self.messages_count - count)

    async def run(self, link, contents, report):
        """`contents`: the message, or the variants each comment takes one of: under the post none twice before
        every one has (variants), whichever worker writes it."""
        texts = variants(contents)
        channel, post_id = self.parse_message_link(link)
        per_worker = self.messages_count  # 0: unlimited, a counter only
        self.progress_total(len(self.sessions) * per_worker if per_worker else None)

        await asyncio.gather(*[
            self.broadcast(session, channel, post_id, texts, report)
            for session in self.sessions
        ])
