from modules.console import console

from functions.base import TelethonFunction
from functions.base.base import AccountLimited, console_report
from modules import rich_message
from modules.rich_message import RichContent


class PmBroadcastFunc(TelethonFunction):
    """Broadcast to PM"""

    async def broadcast(self, session, peer, content, by_phone_number, report):
        async with self.storage.ainitialize_session(session):
            try:
                me = await self.get_me(session)
            except Exception as err:
                await report(f"get_me failed: {err}")
                return

            if by_phone_number:
                try:
                    users = await self.import_phone_contact(session, peer)
                except Exception as err:
                    # one account's failure must not abort the gather for the others
                    await report(f"[{me.first_name}] couldn't resolve phone {peer}: {err}")
                    return

                if not users:
                    await report(f"[{me.first_name}] couldn't resolve phone {peer}")
                    return

                peer = users[0]

            try:
                await rich_message.send(session, peer, content, self.safe_call, report=report)
            except AccountLimited as err:
                await report(f"[{me.first_name}] limit. {err}")
            except Exception as err:
                await report(f"[{me.first_name}] not sent. {err}")
            else:
                await report(f"[{me.first_name}] sent.")

    async def run(self, peer, content, by_phone_number, report):
        await self.run_sequential(
            lambda session, report: self.broadcast(session, peer, content, by_phone_number, report),
            report,
            pause=self.settings.delay,
        )

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

        # CLI path sends plain text only; the bot supplies rich content (media/emoji/formatting).
        await self.run(peer, RichContent(text=text), by_phone_number, console_report)
