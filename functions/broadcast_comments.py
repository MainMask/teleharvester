import asyncio
from rich.prompt import Prompt
from modules.console import console

from functions.base import TelethonFunction
from functions.base.base import AccountLimited, console_report
from modules import rich_message
from modules.rich_message import RichContent


class CommentsBroadcastFunc(TelethonFunction):
    """Broadcast to channel comments"""

    async def broadcast(self, session, channel, post_id, content, report):
        async with self.storage.ainitialize_session(session):
            try:
                me = await session.get_me()
            except Exception as err:
                await report(f"get_me failed: {err}")
                return

            count = 0
            errors = 0

            while count < self.settings.messages_count \
                    or self.settings.messages_count == 0:
                try:
                    await rich_message.send(
                        session, channel, content, self.safe_call,
                        report=report, comment_to=post_id,
                    )
                except AccountLimited as err:
                    await report(f"[{me.first_name}] limit, stopping. {err}")
                    break
                except Exception as err:
                    await report(f"[{me.first_name}] not sent. {err}")

                    errors += 1

                    if errors >= 5:
                        break
                else:
                    count += 1
                    await report(f"[{me.first_name}] sent. COUNT: {count}")
                finally:
                    await self.delay()

    async def run(self, link, content, delay, report):
        self.delay_range = delay

        channel, post_id = self.parse_message_link(link)

        await asyncio.gather(*[
            self.broadcast(session, channel, post_id, content, report)
            for session in self.sessions
        ])

    async def execute(self):
        self.ask_accounts_count()

        link = console.input("[bold red]link to post> [/]")

        delay = Prompt.ask(
            "[bold red]delay[/]",
            default="-".join(str(x) for x in self.settings.delay)
        )

        text = console.input("[bold red]message: [/]")

        # CLI path sends plain text only; the bot supplies rich content (media/emoji/formatting).
        await self.run(link, RichContent(text=text), self.parse_delay(delay), console_report)
