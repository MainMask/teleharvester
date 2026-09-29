import asyncio
import os
import random
from rich.prompt import Prompt, Confirm
from rich.console import Console
from telethon import events, types
from telethon.tl.functions.messages import GetStickerSetRequest
from telethon.tl.types import InputStickerSetShortName

from functions.base import TelethonFunction
from functions.base.base import AccountLimited
console = Console()

class Broadcast(TelethonFunction):
    def __init__(self, storage, settings):
        super().__init__(storage, settings)

        self.choice = None
        self.function = None

        self.modes = (
            ("Campaign with text", self.text_broadcast),
            ("Single-account campaign", self.text_broadcast),
            ("Campaign with media", self.gif_broadcast),
            ("Campaign with reply", self.reply_broadcast),
            ("Campaign with stickers", self.stickers_broadcast)
        )

        self.reply_msg_id = 0

    async def stickers_broadcast(self, session, peer, text):
        stickers = await session(
            GetStickerSetRequest(
                stickerset=InputStickerSetShortName(
                    short_name=self.sticker_set
                )
            )
        )

        await session.send_file(peer, random.choice(stickers.documents))

        await session.send_message(
            peer,
            text,
            parse_mode="html"
        )

    async def text_broadcast(self, session, peer, text):
        await session.send_message(
            peer,
            text,
            parse_mode="html"
        )

    async def reply_broadcast(self, session, peer, text):
        await session.send_message(
            peer,
            text,
            reply_to=self.reply_msg_id,
            parse_mode="html"
        )

    async def gif_broadcast(self, session, peer, text):
        file = random.choice(os.listdir("media"))

        await session.send_file(
            peer,
            os.path.join("media", file),
            caption=text,
            parse_mode="html"
        )

    async def broadcast(self, session, peer, function):
        users = []
        admins = []

        admin_links = []

        count = 0
        errors = 0
        me = await session.get_me()

        if self.mention_all:
            admins = await session.get_participants(
                peer,
                filter=types.ChannelParticipantsAdmins
            )

            if self.mention_mode == "users":
                users = [
                    user for user in await session.get_participants(peer)
                    if user not in admins
                ]

                users_links = [
                    f"<a href=\"tg://user?id={user.id}\">\u206c\u206f</a>"
                    for user in users
                ]

            admin_links = [
                f"<a href=\"tg://user?id={user.id}\">\u206c\u206f</a>"
                for user in admins
            ]


        while count < self.settings.messages_count \
                or self.settings.messages_count == 0:
            if not self.mention_all:
                text = random.choice(self.settings.messages)
            else:
                text = random.choice(self.settings.messages) + \
                    "\u206c\u206f".join(
                        random.sample(users_links, min(len(users_links), 10)) if self.mention_mode == "users"
                        else random.sample(admin_links, min(len(admin_links), 2))
                    )

            try:
                await self.safe_call(lambda: function(session, peer, text))
            except AccountLimited as err:
                console.print(
                    "[{name}] [bold red]limit, stopping.[/] {err}"
                    .format(name=self.safe(me.first_name), err=self.safe(err))
                )
                break
            except Exception as err:
                console.print(
                    "[{name}] [bold red]not sent.[/] [bold white]{err}[/]"
                    .format(name=self.safe(me.first_name), err=self.safe(err))
                )

                errors += 1

                if errors >= 3:
                    try:
                        await session.delete_dialog(peer)
                    except Exception as err:
                        console.print(f"[bold red]ERROR[/] while leaving from chat: {err}")

                    break

            else:
                count += 1
                console.print(
                    "[{name}] [bold green]sent.[/] COUNT: [yellow]{count}[/]"
                    .format(name=self.safe(me.first_name), count=count)
                )
            finally:
                await self.delay()

    async def handle(self, session, function):
        @session.on(events.NewMessage)
        async def handler(message: types.Message):
            if message.raw_text == self.settings.trigger:
                if message.reply_to:
                    self.reply_msg_id = message.reply_to.reply_to_msg_id

                await self.broadcast(
                    session,
                    message.chat_id,
                    function,
                )

        if not self.storage.initialize:
            await session.connect()

        await session.run_until_disconnected()

    def ask(self):
        for index, mode in enumerate(self.modes):
            console.print(
                "[bold white][{index}] {description}[/]"
                .format(index=index + 1, description=mode[0]),
            )

        choice = console.input(
            "[bold white]>> [/]"
        )

        while not choice.isdigit():
            choice = console.input(
                "[bold white]>> [/]"
            )

        else:
            self.choice = int(choice) - 1

        self.function = self.modes[self.choice][1]
        self.ask_accounts_count()

        if self.choice == 4:
            self.sticker_set = console.input("[bold red]enter link to sticker set (e.g https://t.me/addstickers/AlbinoEmoji)> [/]")
            self.sticker_set = self.sticker_set.replace("https://t.me/addstickers/", "")

        delay = Prompt.ask(
            "[bold red]delay[/]",
            default="-".join(str(x) for x in self.settings.delay)
        )

        self.settings.delay = self.parse_delay(delay)
        self.mention_all = Confirm.ask("[bold red]mention all?[/]", default=True)

        if self.mention_all:
            self.mention_mode = Prompt.ask(
                "[bold red]mention mode[/]",
                choices=["admins", "users"]
            )
        
        return self.choice
    
    async def start_single_campaign(self, sessions, link):
        for session in sessions:
            async with self.storage.ainitialize_session(session):
                await self.broadcast(
                    session,
                    link,
                    self.function,
                )

    async def start_campaign(self):
        if self.choice == 1:
            link = Prompt.ask("[bold red]link to chat[/]")

            await self.start_single_campaign(self.sessions, link)
            return

        console.print(
            "[bold white][*] Send \"[green]{trigger}[/]\" to chat[/]"
            .format(trigger=self.settings.trigger)
        )

        await asyncio.gather(*[
            self.handle(session, self.function)
            for session in self.sessions
        ])

