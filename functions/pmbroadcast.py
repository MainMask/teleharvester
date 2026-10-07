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
                self.progress_failed()
                await report(f"не удалось опросить аккаунт: {err}")
                return

            if by_phone_number:
                try:
                    users = await self.import_phone_contact(session, peer)
                except Exception as err:
                    # one account's failure must not abort the gather for the others
                    self.progress_failed()
                    await report(f"[{me.first_name}] не удалось найти номер {peer}: {err}")
                    return

                if not users:
                    self.progress_failed()
                    await report(f"[{me.first_name}] не удалось найти номер {peer}")
                    return

                peer = users[0]

            try:
                await rich_message.send(session, peer, content, self.safe_call, report=report)
            except AccountLimited as err:
                self.progress_failed()
                await report(f"[{me.first_name}] лимит: {err}")
            except Exception as err:
                self.progress_failed()
                await report(f"[{me.first_name}] не отправлено: {err}")
            else:
                self.progress_ok()
                await report(f"[{me.first_name}] отправлено.")

    async def run(self, peer, content, by_phone_number, report):
        await self.run_sequential(
            lambda session, report: self.broadcast(session, peer, content, by_phone_number, report),
            report,
            pause=self.settings.delay,
        )

    async def execute(self):
        self.ask_accounts_count()

        console.print()
        console.print("[bold white][1] По username")
        console.print("[bold white][2] По номеру телефона")
        choice = console.input("\n[bold white]>> ")
        
        by_phone_number = False
        
        if choice == "1":
            peer = console.input("[bold red]username> [/]")
        elif choice == "2":
            by_phone_number = True
            peer = console.input("[bold red]номер телефона> [/]")
        else:
            console.print("[bold red]Неверный выбор!")
            return

        text = console.input("[bold red]текст> [/]")

        # CLI path sends plain text only; the bot supplies rich content (media/emoji/formatting).
        await self.run(peer, RichContent(text=text), by_phone_number, console_report)
