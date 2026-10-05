from modules.console import console

from telethon import functions
from functions.base import TelethonFunction
from functions.base.base import console_report


class PollVoteFunc(TelethonFunction):
    """Vote in poll"""

    async def vote(self, session, channel, post_id, option_number, report):
        async with self.storage.ainitialize_session(session):
            try:
                me = await session.get_me()
            except Exception as err:
                await report(f"get_me failed: {err}")
                return False

            try:
                message = await session.get_messages(channel, ids=post_id)
                option = message.poll.poll.answers[option_number].option

                # safe_call waits out short FloodWaits so the vote lands; a long wait
                # raises AccountLimited, caught by the except below and reported.
                await self.safe_call(lambda: session(
                    functions.messages.SendVoteRequest(
                        peer=channel,
                        msg_id=post_id,
                        options=[option]
                    )
                ))
            except Exception as err:
                await report(f"[{me.first_name}] not voted: {err}")
                return False

            await report(f"[{me.first_name}] voted")
            return True

    async def run(self, link, option_number, report):
        channel, post_id = self.parse_message_link(link)

        results = await self.gather_in_order(
            lambda session, report: self.vote(session, channel, post_id, option_number, report),
            report,
        )

        # no ok/error keyword: the per-account lines above are what the job summary counts
        await report(f"Done: {sum(results)}/{len(self.sessions)} accounts")

    async def execute(self):
        self.ask_accounts_count()

        post_link = console.input("[bold red]enter link to msg/post> ")
        option_number = console.input("[bold red]enter answer number (e.g 1, 2)> ")

        while not (option_number.isdigit() and int(option_number) >= 1):
            option_number = console.input("[bold red]enter answer number (e.g 1, 2)> ")

        option_number = int(option_number) - 1

        with console.status("Voting"):
            await self.run(post_link, option_number, console_report)

        
