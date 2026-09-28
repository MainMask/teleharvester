import asyncio
from rich.console import Console

from telethon import functions
from functions.base import TelethonFunction

console = Console()


class PollVoteFunc(TelethonFunction):
    """Vote in poll"""

    async def vote(self, session, channel, post_id, option_number):
        async with self.storage.ainitialize_session(session):
            try:
                message = await session.get_messages(channel, ids=post_id)
                option = message.poll.poll.answers[option_number].option

                await session(
                    functions.messages.SendVoteRequest(
                        peer=channel,
                        msg_id=post_id,
                        options=[option]
                    )
                )
            except Exception as err:
                console.print(f"[bold red][!][/] {err}")

    async def execute(self):
        self.ask_accounts_count()

        post_link = console.input("[bold red]enter link to msg/post> ")
        option_number = int(console.input("[bold red]enter answer number (e.g 1, 2)> ")) - 1

        channel, post_id = self.parse_message_link(post_link)

        with console.status("Voting"):
            await asyncio.gather(*[
                self.vote(session, channel, post_id, option_number)
                for session in self.sessions
            ])

        
