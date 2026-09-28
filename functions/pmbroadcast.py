import asyncio
import random
import os
from telethon import functions, types
from rich.prompt import Prompt, Confirm
from rich.console import Console

from functions.base import TelethonFunction
from functions.base.base import AccountLimited

console = Console()


class PmBroadcastFunc(TelethonFunction):
    """Broadcast to PM"""

    async def broadcast(self, session, peer, text, media, by_phone_number):
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
                
                peer = result.users[0]

            while True:
                try:
                    if not media:
                        await self.safe_call(lambda: session.send_message(peer, text))
                    else:
                        file = random.choice(os.listdir("media"))
                        path = os.path.join("media", file)

                        await self.safe_call(lambda: session.send_file(
                            peer,
                            path,
                            caption=text,
                            parse_mode="html"
                        ))
                except AccountLimited as err:
                    console.print(
                        "[{name}] [bold red]limit, stopping.[/] {err}"
                        .format(name=me.first_name, err=err)
                    )
                    break
                except Exception as err:
                    console.print(
                        "[{name}] [bold red]not sent.[/] {err}"
                        .format(name=me.first_name, err=err)
                    )

                    if errors >= 5:
                        break

                    errors += 1
                else:
                    count += 1
                    console.print(
                        "[{name}] [bold green]sent.[/] COUNT: [yellow]{count}[/]"
                        .format(name=me.first_name, count=count)
                    )
                finally:
                    await self.delay()

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

        media = Confirm.ask("[bold red]media")
        text = console.input("[bold red]text> [/]")

        delay = Prompt.ask(
            "[bold red]delay[/]",
            default="-".join(str(x) for x in self.settings.delay)
        )

        self.settings.delay = self.parse_delay(delay)

        await asyncio.gather(*[
            self.broadcast(session, peer, text, media, by_phone_number=by_phone_number)
            for session in self.sessions
        ])
