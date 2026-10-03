import asyncio

from rich.progress import track
from modules.console import console
from rich.prompt import Prompt, Confirm

from time import perf_counter

from telethon import events, types
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.sync import TelegramClient

from functions.broadcast import Broadcast
from functions.base import TelethonFunction
from functions.base.base import AccountLimited, console_report


class JoinerFunc(TelethonFunction):
    """Join chat"""

    async def join(self, session, link, index, mode, report=None):
        async def emit(msg):
            if report is not None:
                await report(msg)
            else:
                print(msg)

        if mode == "1":
            try:
                if not "joinchat" in link:
                    updates = await self.safe_call(lambda: session(JoinChannelRequest(link)))
                else:
                    invite = link.split("/")[-1]
                    updates = await self.safe_call(lambda: session(ImportChatInviteRequest(invite)))
            except AccountLimited as error:
                await emit(f"[-] [acc {index + 1}] limit: {error}")
                return False
            except Exception as error:
                await emit(f"[-] [acc {index + 1}] {error}")
            else:
                # the joined chat for the follow-up broadcast (a joinchat link is no peer);
                # the result wraps the Updates on this layer, UpdatesTooLong has no chats
                chats = getattr(getattr(updates, "updates", updates), "chats", None)
                return chats[0] if chats else link

        elif mode == "2":
            try:
                full = await self.safe_call(lambda: session(GetFullChannelRequest(link)))
                linked_id = full.full_chat.linked_chat_id

                if not linked_id:
                    await emit(f"[-] [acc {index + 1}] no linked chat")
                    return False

                chat = next((c for c in full.chats if c.id == linked_id), None)

                if chat is None:
                    await emit(f"[-] [acc {index + 1}] linked chat not found")
                    return False

                await self.safe_call(lambda: session(JoinChannelRequest(chat)))
            except AccountLimited as error:
                await emit(f"[-] [acc {index + 1}] limit: {error}")
                return False
            except Exception as error:
                await emit(f"[-] [acc {index + 1}] {error}")
            else:
                return chat  # the linked chat, not the channel `link` points to

    async def run(self, mode, link, delay, report):
        """Simplified join for the bot: join `mode` into `link` on every worker."""
        self.delay_range = delay
        link = link.replace("+", "joinchat/")

        joined = 0

        for index, session in enumerate(self.sessions):
            async with self.storage.ainitialize_session(session):
                if await self.join(session, link, index, mode, report):
                    joined += 1

            await self.delay()

        await report(f"[+] {joined}/{len(self.sessions)} accounts joined")

    def solve_captcha(self, session: TelegramClient):
        # just a handler on the already-connected client: run_until_disconnected() here
        # would disconnect the session on cancel (its finally calls disconnect())
        session.add_event_handler(
            self.on_message,
            events.NewMessage
        )

    async def on_message(self, msg: types.Message):
        if not msg.mentioned:
            return
        # MessageButton.data: callback bytes, None for URL / reply / other buttons
        # (hides this layer's KeyboardButton(type=InlineButtonTypeCallback) layout)
        buttons = await msg.get_buttons()
        data = buttons[0][0].data if buttons and buttons[0] else None
        if data:
            await msg.click(data=data)  # raw bytes: the data need not be UTF-8

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

        while mode not in ("1", "2"):
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
        captcha_sessions = []
        targets = []  # per session: the chat it joined (or the link) for the broadcast

        # finally: Ctrl-C or an error must not leave the captcha handler clicking buttons
        # (initialize=True clients stay alive for the next menu functions)
        try:
            if speed == "normal":
                delay = self.ask_int("[bold red]delay[/]", default=0, min_value=0)
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
                            self.solve_captcha(session)
                            captcha_sessions.append(session)

                        is_joined = await self.join(session, link, index, mode)
                        targets.append(is_joined or link)

                        if is_joined:
                            joined += 1

                        await asyncio.sleep(delay)

                elif function_index == 1:
                    for index, session in enumerate(self.sessions):
                        await session.start()

                        if captcha:
                            self.solve_captcha(session)
                            captcha_sessions.append(session)

                        is_joined = await self.join(session, link, index, mode)

                        console.print("[bold green]Account joined[/]")

                        if is_joined:
                            joined += 1
                    
                        console.print("[bold white]Starting broadcast[/]")

                        await broadcast_func.broadcast(session, is_joined or link, console_report)
                        await asyncio.sleep(delay)

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

                targets = [result or link for result in results]

                if broadcast and function_index == 1:
                    for session, target in zip(self.sessions, targets):
                        await broadcast_func.broadcast(session, target, console_report)


            joined_time = round(perf_counter() - start, 2)
            console.print(f"[+] {joined} accounts joined in [yellow]{joined_time}[/]s")

            if broadcast and function_index != 1:
                await asyncio.gather(*[
                    broadcast_func.broadcast(session, target, console_report)
                    for session, target in zip(self.sessions, targets)
                ])
        finally:
            for session in captcha_sessions:
                session.remove_event_handler(self.on_message, events.NewMessage)

            if not self.storage.initialize:
                for session in self.sessions:
                    await session.disconnect()
