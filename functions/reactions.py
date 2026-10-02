import asyncio
import random

from telethon import functions, types
from functions.base import TelethonFunction
from functions.base.base import console_report
from modules.console import console


class ReactionsFunc(TelethonFunction):
    """Set reactions to message/post"""

    reactions = ['👍', '❤️', '🔥', '🥰', '👏', '😁', '🎉', '🤩', '👎', '🤯', '😱', '🤬', '😢', '🤮', '💩', '🙏']

    async def set_reaction(self, session, peer, message_id, report, reaction=None):
        if not reaction:
            reaction = random.choice(self.reactions)

        async with self.storage.ainitialize_session(session):
            try:
                me = await session.get_me()
            except Exception as err:
                await report(f"get_me failed: {err}")
                return

            try:
                # safe_call waits out short FloodWaits so the reaction actually lands
                # (mass reactions on one post trip rate limits); a long wait raises
                # AccountLimited, caught by the except below and reported.
                await self.safe_call(lambda: session(functions.messages.SendReactionRequest(
                    peer=peer,
                    msg_id=int(message_id),
                    reaction=[types.ReactionEmoji(emoticon=reaction)]
                )))
            except Exception as err:
                await report(f"[ERROR] [{me.first_name}] : {err}")
            else:
                await report(f"[SUCCESS] [{me.first_name}] : Reaction \"{reaction}\" was sent")

    async def run(self, link, reaction, report):
        peer, message_id = self.parse_message_link(link)

        await asyncio.gather(*[
            self.set_reaction(session, peer, message_id, report, reaction=reaction)
            for session in self.sessions
        ])

    async def execute(self):
        self.ask_accounts_count()

        link_to_message = console.input("[bold red]link to msg/post> [/]")

        reaction = console.input(
            "[bold red]enter reaction ({reactions}) or skip for random> [/]"
            .format(reactions=", ".join(self.reactions))
        )

        await self.run(link_to_message, reaction, console_report)
