import asyncio
import random
from telethon import functions, types
from rich.prompt import Prompt
from rich.console import Console

from functions.base import TelethonFunction
from functions.base.base import AccountLimited, console_report
from modules import rich_message
from modules.rich_message import RichContent

console = Console()


class PmBroadcastFunc(TelethonFunction):
    """Broadcast to PM"""

    async def broadcast(self, session, peer, content, by_phone_number, report):
        count = 0
        errors = 0

        async with self.storage.ainitialize_session(session):
            try:
                me = await session.get_me()
            except Exception:
                return

            if by_phone_number:
                result = await session(functions.contacts.ImportContactsRequest(
                    contacts=[types.InputPhoneContact(
                        client_id=random.randrange(-2**63, 2**63),
                        phone=peer,
                        first_name='contact',
                        last_name=''
                    )]
                ))

                if not result.users:
                    await report(f"[{me.first_name}] couldn't resolve phone {peer}")
                    return

                peer = result.users[0]

            while True:
                try:
                    await rich_message.send(session, peer, content, self.safe_call, report=report)
                except AccountLimited as err:
                    await report(f"[{me.first_name}] limit, stopping. {err}")
                    break
                except Exception as err:
                    await report(f"[{me.first_name}] not sent. {err}")

                    if errors >= 5:
                        break

                    errors += 1
                else:
                    count += 1
                    await report(f"[{me.first_name}] sent. COUNT: {count}")
                finally:
                    await self.delay()

    async def run(self, peer, content, by_phone_number, delay, report):
        self.delay_range = delay

        await asyncio.gather(*[
            self.broadcast(session, peer, content, by_phone_number, report)
            for session in self.sessions
        ])

    async def execute(self):
        self.ask_accounts_count()

        console.print()
        console.print("[bold white][1] Broadcast by username")
        console.print("[bold white][2] Broadcast by phone number")
        choice = console.input("\n[bold white]>> ")
        
        by_phone_number = False
        
        if choice == "1":
            peer = console.input("[bold red]enter username> [/]")
        elif choice == "2":
            by_phone_number = True
            peer = console.input("[bold red]enter phone number> [/]")
        else:
            console.print("[bold red]Invalid input!")
            return

        text = console.input("[bold red]text> [/]")

        delay = Prompt.ask(
            "[bold red]delay[/]",
            default="-".join(str(x) for x in self.settings.delay)
        )

        # CLI path sends plain text only; the bot supplies rich content (media/emoji/formatting).
        await self.run(
            peer, RichContent(text=text), by_phone_number, self.parse_delay(delay), console_report
        )
