import asyncio
import random

from telethon import functions, types
from functions.base import TelethonFunction
from rich.console import Console

console = Console()


class ReactionsFunc(TelethonFunction):
    """Set reactions to message/post"""

    reactions = ['👍', '❤️', '🔥', '🥰', '👏', '😁', '🎉', '🤩', '👎', '🤯', '😱', '🤬', '😢', '🤮', '💩', '🙏']

    async def set_reaction(self, session, peer, message_id, reaction=None):
        if not reaction:
            reaction = random.choice(self.reactions)

        async with self.storage.ainitialize_session(session):
            try:
                me = await session.get_me()
            except Exception as err:
                console.print(f"[bold red]get_me failed:[/] {self.safe(err)}")
                return

            try:
                await session(functions.messages.SendReactionRequest(
                    peer=peer,
                    msg_id=int(message_id),
                    reaction=[types.ReactionEmoji(emoticon=reaction)]
                ))
            except Exception as err:
                console.print(f"[bold red][ERROR][/] [bold yellow][{self.safe(me.first_name)}][/] : {self.safe(err)}")
            else:
                console.print(f"[bold green][SUCCESS] [{self.safe(me.first_name)}][/] : Reaction \"{reaction}\" was sent")

    async def execute(self):
        self.ask_accounts_count()

        link_to_message = console.input("[bold red]link to msg/post> [/]")
        peer, message_id = self.parse_message_link(link_to_message)

        reaction = console.input(
            "[bold red]enter reaction ({reactions}) or skip for random> [/]"
            .format(reactions=", ".join(self.reactions))
        )

        await asyncio.gather(*[
            self.set_reaction(
                session=session,
                peer=peer,
                message_id=message_id,
                reaction=reaction
            )
            for session in self.sessions
        ])
