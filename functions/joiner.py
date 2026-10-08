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
from scraper.scrape import channel_slug

CAPTCHA_WAIT = 30  # seconds a worker waits for a captcha after joining (bot mode)


class JoinerFunc(TelethonFunction):
    """Join chat"""

    async def join(self, session, link, index, mode, report=None):
        async def emit(msg):
            if report is not None:
                await report(msg)
            else:
                print(msg)

        # channel_slug: the scraper's parser, which drops a trailing "/", a ?query and a post id
        slug = channel_slug(link)

        if mode == "1":
            try:
                if not slug.startswith("+"):  # an invite's slug is "+<hash>"
                    updates = await self.safe_call(lambda: session(JoinChannelRequest("@" + slug)))
                else:
                    updates = await self.safe_call(lambda: session(ImportChatInviteRequest(slug[1:])))
            except AccountLimited as error:
                await emit(f"[!] [аккаунт {index + 1}] лимит: {error}")
                return False
            except Exception as error:
                await emit(f"[!] [аккаунт {index + 1}] {error}")
            else:
                if isinstance(updates, types.messages.ChatInviteJoinResultWebView):
                    await emit(f"[!] [аккаунт {index + 1}] чат требует подтверждения вступления в web-app бота")
                    return False
                # the joined chat for the follow-up broadcast (an invite link is no peer);
                # the result wraps the Updates on this layer, UpdatesTooLong has no chats
                chats = getattr(getattr(updates, "updates", updates), "chats", None)
                return chats[0] if chats else link

        elif mode == "2":
            try:
                full = await self.safe_call(lambda: session(GetFullChannelRequest("@" + slug)))
                linked_id = full.full_chat.linked_chat_id

                if not linked_id:
                    await emit(f"[!] [аккаунт {index + 1}] у канала нет привязанного чата")
                    return False

                chat = next((c for c in full.chats if c.id == linked_id), None)

                if chat is None:
                    await emit(f"[!] [аккаунт {index + 1}] привязанный чат не найден")
                    return False

                await self.safe_call(lambda: session(JoinChannelRequest(chat)))
            except AccountLimited as error:
                await emit(f"[!] [аккаунт {index + 1}] лимит: {error}")
                return False
            except Exception as error:
                await emit(f"[!] [аккаунт {index + 1}] {error}")
            else:
                return chat  # the linked chat, not the channel `link` points to

    async def join_with_captcha(self, session, link, index, mode, report):
        """Join, then wait up to CAPTCHA_WAIT for a captcha and click it."""
        clicked = asyncio.Event()

        async def handler(msg):
            if await self.on_message(msg):
                clicked.set()

        # registered before joining: the captcha can arrive right after the join
        session.add_event_handler(handler, events.NewMessage)

        try:
            joined = await self.join(session, link, index, mode, report)

            if joined:
                await report(f"[аккаунт {index + 1}] вступил")
                try:
                    await asyncio.wait_for(clicked.wait(), CAPTCHA_WAIT)
                except asyncio.TimeoutError:
                    await report(f"[аккаунт {index + 1}] капчи не было {CAPTCHA_WAIT} с")
                else:
                    await report(f"[аккаунт {index + 1}] капча пройдена")

            return joined
        finally:
            session.remove_event_handler(handler, events.NewMessage)

    async def run(self, mode, link, delay, report, captcha=False):
        """Simplified join for the bot: join `mode` into `link` on every worker."""
        self.delay_range = delay

        joined = 0
        self.progress_total(len(self.sessions))

        for index, session in enumerate(self.sessions):
            async with self.storage.ainitialize_session(session):
                if captcha:
                    is_joined = await self.join_with_captcha(session, link, index, mode, report)
                else:
                    is_joined = await self.join(session, link, index, mode, report)
                    if is_joined:
                        await report(f"[аккаунт {index + 1}] вступил")

                if is_joined:
                    joined += 1
                    self.progress_ok()
                else:  # join() reported why
                    self.progress_failed()

            self.progress_step()
            if index < len(self.sessions) - 1:  # no trailing wait after the last account
                await self.delay()

        await report(f"Итого: {joined}/{len(self.sessions)} аккаунтов")

    def solve_captcha(self, session: TelegramClient):
        # just a handler on the already-connected client: run_until_disconnected() here
        # would disconnect the session on cancel (its finally calls disconnect())
        session.add_event_handler(
            self.on_message,
            events.NewMessage
        )

    async def on_message(self, msg: types.Message):
        """Click the first callback button of a message mentioning this account; True if clicked."""
        if not msg.mentioned:
            return False
        # MessageButton.data: callback bytes, None for URL / reply / other buttons
        # (hides this layer's KeyboardButton(type=InlineButtonTypeCallback) layout)
        buttons = await msg.get_buttons()
        data = buttons[0][0].data if buttons and buttons[0] else None
        if data:
            await msg.click(data=data)  # raw bytes: the data need not be UTF-8
            return True
        return False

    async def execute(self):
        self.ask_accounts_count()

        print()

        console.print(
            "[1] Просто вступить в чат/канал",
            "[2] Вступить в чат, привязанный к каналу",
            sep="\n",
            style="bold white"
        )

        print()

        mode = console.input("[bold red]режим> [/]")

        while mode not in ("1", "2"):
            mode = console.input("[bold red]режим> [/]")

        link = console.input("[bold red]ссылка> [/]")

        speed = Prompt.ask(
            "[bold red]скорость (normal — по очереди, fast — все сразу)>[/]",
            choices=["normal", "fast"]
        )

        broadcast = Confirm.ask("[bold red]сразу разослать сообщения?[/]")

        if broadcast:
            broadcast_func = Broadcast(self.storage, self.settings)
            function_index = broadcast_func.ask(modes=(0, 1, 4))  # no trigger message to reply to

        else:
            function_index = None

        joined = 0
        captcha_sessions = []
        targets = []  # per session: the chat it joined (or the link) for the broadcast

        # finally: Ctrl-C or an error must not leave the captcha handler clicking buttons
        # (initialize=True clients stay alive for the next menu functions)
        try:
            if speed == "normal":
                delay = self.ask_int("[bold red]задержка[/]", default=0, min_value=0)
                captcha = Confirm.ask("[bold red]проходить капчу?[/]")

                start = perf_counter()

                # the single-account campaign broadcasts right after each join, so its
                # lines would break a progress bar: it gets plain prints instead
                inline = function_index == 1
                sessions = enumerate(self.sessions)
                if not inline:
                    sessions = track(sessions, "[yellow]Вступление[/]", total=len(self.sessions))

                for index, session in sessions:
                    await session.start()

                    if captcha:
                        self.solve_captcha(session)
                        captcha_sessions.append(session)

                    is_joined = await self.join(session, link, index, mode)
                    targets.append(is_joined or link)

                    if is_joined:
                        joined += 1

                    if inline:
                        if is_joined:
                            console.print("[bold green]Аккаунт вступил[/]")
                        console.print("[bold white]Начинаю рассылку[/]")
                        await broadcast_func.broadcast(session, is_joined or link, console_report)

                    await asyncio.sleep(delay)

            if speed == "fast":
                if not self.storage.initialize:
                    for session in track(
                        self.sessions,
                        "[yellow]Подключение сессий[/]",
                        total=len(self.sessions)
                    ):
                        await session.connect()

                with console.status("Вступление"):
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
            console.print(f"[+] Вступило аккаунтов: {joined} за [yellow]{joined_time}[/] с")

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
