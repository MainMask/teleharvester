import random

from telethon import functions, types
from functions.base import TelethonFunction
from functions.base.base import console_report
from modules.console import console


class ReactionsFunc(TelethonFunction):
    """Set reactions to message/post"""

    reactions = ['👍', '❤️', '🔥', '🥰', '👏', '😁', '🎉', '🤩', '👎', '🤯', '😱', '🤬', '😢', '🤮', '💩', '🙏']

    async def set_reaction(self, session, peer, message_id, report, reaction=None, comment=None):
        if not reaction:
            reaction = random.choice(self.reactions)
        # Telegram's reaction emoticons carry no variation selector: "❤️" is REACTION_INVALID
        reaction = reaction.replace("\ufe0f", "")

        async with self.storage.ainitialize_session(session):
            me = await self.poll_me(session, report)
            if me is None:
                return

            try:
                peer, message_id = await self.resolve_message(session, peer, message_id, comment)
                # safe_call waits out short FloodWaits so the reaction actually lands
                # (mass reactions on one post trip rate limits); a long wait raises
                # AccountLimited, caught by the except below and reported.
                await self.safe_call(lambda: session(functions.messages.SendReactionRequest(
                    peer=peer,
                    msg_id=int(message_id),
                    reaction=[types.ReactionEmoji(emoticon=reaction)]
                )))
            except Exception as err:
                self.progress_failed()
                await report(f"[{me.first_name}] ошибка: {err}")
            else:
                self.progress_ok()
                await report(f"[{me.first_name}] реакция «{reaction}» поставлена")

    async def run(self, link, reaction, report):
        peer, message_id = self.parse_message_link(link)
        comment = self.comment_id(link)  # a comment's link: the reaction goes on the comment

        await self.run_sequential(
            lambda session, report: self.set_reaction(session, peer, message_id, report,
                                                      reaction=reaction, comment=comment),
            report,
            pause="delay",
        )

    async def execute(self):
        link_to_message = self.ask_message_link("[bold red]ссылка на сообщение/пост> [/]")

        # as the bot's buttons: one of the list (its number, or itself), or a random one each
        console.print("  ".join(f"[{number}] {emoji}" for number, emoji in enumerate(self.reactions, 1)),
                      markup=False, highlight=False)
        listed = {emoji.replace("\ufe0f", ""): emoji for emoji in self.reactions}  # a terminal may drop the selector
        while True:
            raw = console.input("[bold red]реакция — номер или сама реакция, пусто — случайная у каждого> [/]").strip()
            if raw.isdigit() and 1 <= int(raw) <= len(self.reactions):
                reaction = self.reactions[int(raw) - 1]
                break
            if not raw or (key := raw.replace("\ufe0f", "")) in listed:
                reaction = listed[key] if raw else ""  # "": a random one each (run)
                break
            console.print("Такой реакции нет в списке: номер из него или пусто.", style="bold red", markup=False)

        await self.run(link_to_message, reaction, console_report)
