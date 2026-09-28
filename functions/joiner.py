import random
import asyncio

from rich.progress import track
from rich.console import Console
from rich.prompt import Prompt, Confirm

from time import perf_counter

from telethon import events, types
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.sync import TelegramClient

from functions.broadcast import Broadcast
from functions.base import TelethonFunction
from functions.base.base import AccountLimited

console = Console()


class JoinerFunc(TelethonFunction):
    """Join chat"""

    async def join(self, session, link, index, mode):
        if mode == "1":
            try:
                if not "joinchat" in link:
                    await self.safe_call(lambda: session(JoinChannelRequest(link)))
                else:
                    invite = link.split("/")[-1]
                    await self.safe_call(lambda: session(ImportChatInviteRequest(invite)))
            except AccountLimited as error:
                print(f"[-] [acc {index + 1}] limit: {error}")
                return False
            except Exception as error:
                print(f"[-] [acc {index + 1}] {error}")
            else:
                return True

        elif mode == "2":
            try:
                channel = await self.safe_call(lambda: session(GetFullChannelRequest(link)))
                chat = channel.chats[1]
                await self.safe_call(lambda: session(JoinChannelRequest(chat)))
            except AccountLimited as error:
                print(f"[-] [acc {index + 1}] limit: {error}")
                return False
            except Exception as error:
                print(f"[-] [acc {index + 1}] {error}")
            else:
                return True

    async def solve_captcha(self, session: TelegramClient):
        session.add_event_handler(
            self.on_message,
            events.NewMessage
        )

        await session.run_until_disconnected()

    async def on_message(self, msg: types.Message):
        if msg.mentioned:
            if msg.reply_markup:
                captcha = msg.reply_markup.rows[0] \
                    .buttons[0].data.decode("utf-8")

                await msg.click(data=captcha)

    async def execute(self):
        self.ask_accounts_count()

        print()

        console.print(
            "[1] Just join chat/channel",
            "[2] Join linked to channel chat",
            sep="\n",
            style="bold white"
        )

        print()

        mode = console.input("[bold red]mode> [/]")
        link = console.input("[bold red]link> [/]")
        
        link = link.replace("+", "joinchat/")

        speed = Prompt.ask(
            "[bold red]speed>[/]",
            choices=["normal", "fast"]
        )

        broadcast = Confirm.ask("[bold red]broadcast instantly?[/]")

        if broadcast:
            broadcast_func = Broadcast(self.storage, self.settings)
            function_index = broadcast_func.ask()

        else:
            function_index = None

        joined = 0
        captcha_tasks = []

        if speed == "normal":
            delay = Prompt.ask("[bold red]delay[/]", default="0")
            captcha = Confirm.ask("[bold red]captcha[/]")

            start = perf_counter()

            if function_index != 1:
                for index, session in track(
                    enumerate(self.sessions),
                    "[yellow]Joining[/]",
                    total=len(self.sessions)
                ):
                    await session.start()

                    if captcha:
                        captcha_tasks.append(
                            asyncio.create_task(self.solve_captcha(session))
                        )

                    is_joined = await self.join(session, link, index, mode)

                    if is_joined:
                        joined += 1

                    await asyncio.sleep(int(delay))
            
            elif function_index == 1:
                for index, session in enumerate(self.sessions):
                    await session.start()

                    if captcha:
                        captcha_tasks.append(
                            asyncio.create_task(self.solve_captcha(session))
                        )

                    is_joined = await self.join(session, link, index, mode)

                    console.print("[bold green]Account joined[/]")

                    if is_joined:
                        joined += 1
                    
                    console.print("[bold white]Starting broadcast[/]")

                    await broadcast_func.broadcast(session, link, broadcast_func.function)
                    await asyncio.sleep(int(delay))

        if speed == "fast":
            if not self.storage.initialize:
                for session in track(
                    self.sessions,
                    "[yellow]Initializing sessions[/]",
                    total=len(self.sessions)
                ):
                    await session.connect()

            with console.status("Joining"):
                start = perf_counter()

                results = await asyncio.gather(*[
                    self.join(session, link, index, mode)
                    for index, session in enumerate(self.sessions)
                ])

            for result in results:
                if result:
                    joined += 1

            if broadcast and function_index == 1:
                for session in self.sessions:
                    await broadcast_func.broadcast(session, link, broadcast_func.function)


        joined_time = round(perf_counter() - start, 2)
        console.print(f"[+] {joined} accounts joined in [yellow]{joined_time}[/]s")

        if broadcast and function_index != 1:
            await asyncio.gather(*[
                broadcast_func.broadcast(session, link, broadcast_func.function)
                for session in self.sessions
            ])

        if not self.storage.initialize:
            for session in self.sessions:
                await session.disconnect()
