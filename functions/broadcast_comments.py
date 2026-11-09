import asyncio


from functions.base import TelethonFunction
from functions.base.base import AccountLimited
from modules import rich_message


class CommentsBroadcastFunc(TelethonFunction):
    """Broadcast to channel comments"""

    def __init__(self, storage, settings):
        super().__init__(storage, settings)
        # the run's own, as it was started: config.toml's is every project's, another project's run may change it
        self.messages_count = settings.messages_count

    async def broadcast(self, session, channel, post_id, content, report):
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

            while count < self.messages_count \
                    or self.messages_count == 0:
                try:
                    await rich_message.send(
                        session, channel, content, self.safe_call,
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

    async def run(self, link, content, report):
        channel, post_id = self.parse_message_link(link)
        per_worker = self.messages_count  # 0: unlimited, a counter only
        self.progress_total(len(self.sessions) * per_worker if per_worker else None)

        await asyncio.gather(*[
            self.broadcast(session, channel, post_id, content, report)
            for session in self.sessions
        ])
