import os
import random
import asyncio
from rich.prompt import Prompt, Confirm
from rich.console import Console

from functions.base import TelethonFunction
from functions.base.base import AccountLimited
console = Console()


class CommentsBroadcastFunc(TelethonFunction):
    """Broadcast to channel comments"""

    async def broadcast(self, session, channel, post_id, media):
        async with self.storage.ainitialize_session(session):
            me = await session.get_me()
            count = 0
            errors = 0

            while count < self.settings.messages_count \
                    or self.settings.messages_count == 0:
                text = random.choice(self.settings.messages)

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
                    console.print(
                        "[{name}] [bold red]limit, stopping.[/] {err}"
                        .format(name=self.safe(me.first_name), err=self.safe(err))
                    )
                    break
                except Exception as err:
                    console.print(
                        "[{name}] [bold red]not sent.[/] {err}"
                        .format(name=self.safe(me.first_name), err=self.safe(err))
                    )

                    errors += 1

                    if errors >= 5:
                        break
                else:
                    count += 1
                    console.print(
                        "[{name}] [bold green]sent.[/] COUNT: [yellow]{count}[/]"
                        .format(name=self.safe(me.first_name), count=count)
                    )
                finally:
                    await self.delay()

    async def execute(self):
        self.ask_accounts_count()

        link = console.input("[bold red]link to post> [/]")

        delay = Prompt.ask(
            "[bold red]delay[/]",
            default="-".join(str(x) for x in self.settings.delay)
        )

        media = Confirm.ask("[bold red]media[/]")
        from_config = Confirm.ask("[bold red]use messages from config?[/]")

        if not from_config:
            self.settings.messages = [console.input("[bold red]message: [/]")]

        self.settings.delay = self.parse_delay(delay)

        channel = "/" .join(link.split("/")[:-1])
        post_id = link.split("/")[-1]

        await asyncio.gather(*[
            self.broadcast(session, channel, int(post_id), media)
            for session in self.sessions
        ])
