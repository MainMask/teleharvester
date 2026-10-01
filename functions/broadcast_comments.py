import os
import random
import asyncio
from rich.prompt import Prompt, Confirm
from rich.console import Console

from functions.base import TelethonFunction
from functions.base.base import AccountLimited, console_report
console = Console()


class CommentsBroadcastFunc(TelethonFunction):
    """Broadcast to channel comments"""

    async def broadcast(self, session, channel, post_id, media, messages, report):
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
                text = random.choice(messages)

                try:
                    if not media:
                        await self.safe_call(lambda: session.send_message(
                            channel,
                            text,
                            comment_to=post_id,
                            parse_mode="html"
                        ))
                    else:
                        file = random.choice(os.listdir("media"))
                        path = os.path.join("media", file)

                        await self.safe_call(lambda: session.send_file(
                            channel,
                            path,
                            comment_to=post_id,
                            caption=text,
                            parse_mode="html"
                        ))
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

    async def run(self, link, media, messages, delay, report):
        self.delay_range = delay

        channel = "/".join(link.split("/")[:-1])
        post_id = int(link.split("/")[-1])

        await asyncio.gather(*[
            self.broadcast(session, channel, post_id, media, messages, report)
            for session in self.sessions
        ])

    async def execute(self):
        self.ask_accounts_count()

        link = console.input("[bold red]link to post> [/]")

        delay = Prompt.ask(
            "[bold red]delay[/]",
            default="-".join(str(x) for x in self.settings.delay)
        )

        media = Confirm.ask("[bold red]media[/]")
        from_config = Confirm.ask("[bold red]use messages from config?[/]")

        if from_config:
            messages = self.settings.messages
        else:
            messages = [console.input("[bold red]message: [/]")]

        await self.run(link, media, messages, self.parse_delay(delay), console_report)
