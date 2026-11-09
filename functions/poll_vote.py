

from telethon import functions
from functions.base import TelethonFunction


class PollVoteFunc(TelethonFunction):
    """Vote in poll"""

    async def vote(self, session, channel, post_id, option_number, report, comment=None):
        async with self.storage.ainitialize_session(session):
            me = await self.poll_me(session, report)
            if me is None:
                return False

            try:
                channel, post_id = await self.resolve_message(session, channel, post_id, comment)
                message = await session.get_messages(channel, ids=post_id)
                option = message.poll.poll.answers[option_number].option

                # safe_call waits out short FloodWaits so the vote lands; a long wait
                # raises AccountLimited, caught by the except below and reported.
                await self.call(session,
                    functions.messages.SendVoteRequest(
                        peer=channel,
                        msg_id=post_id,
                        options=[option]
                    )
                )
            except Exception as err:
                self.progress_failed()
                await report(f"[{me.first_name}] не проголосовал: {err}")
                return False

            self.progress_ok()
            await report(f"[{me.first_name}] проголосовал")
            return True

    async def answers(self, session, link) -> list[str]:
        """The poll's options as its voters see them (the bot offers them as buttons), read on `session`."""
        channel, post_id = await self.resolve_message(session, *self.parse_message_link(link), self.comment_id(link))
        message = await session.get_messages(channel, ids=post_id)
        if message is None or message.poll is None:
            raise ValueError("в этом сообщении нет опроса")
        return [answer.text.text for answer in message.poll.poll.answers]

    async def run(self, link, option_number, report):
        channel, post_id = self.parse_message_link(link)
        comment = self.comment_id(link)  # a poll in a comment: its link carries ?comment=

        results = await self.run_sequential(
            lambda session, report: self.vote(session, channel, post_id, option_number, report, comment),
            report,
            pause="delay",
        )

        # no ok/error keyword: the per-account lines above are what the job summary counts
        await report(f"Итого: {sum(results)}/{len(self.sessions)} аккаунтов")

        
