import asyncio
import random
from rich.prompt import Prompt, Confirm
from modules.console import console
from telethon import events, types
from telethon.extensions import html
from telethon.tl.functions.messages import GetStickerSetRequest
from telethon.tl.types import InputStickerSetShortName

from functions.base import TelethonFunction
from functions.base.base import AccountLimited
from modules import rich_message
from modules.rich_message import RichContent

# Invisible mention carrier: each hidden mention is these two chars linked to a user.
_MENTION_CHARS = "⁬⁯"


def _utf16_len(text: str) -> int:
    """Length in UTF-16 code units (matches Telegram/Telethon entity offsets)."""
    return len(text.encode("utf-16-le")) // 2


class Broadcast(TelethonFunction):
    def __init__(self, storage, settings):
        super().__init__(storage, settings)

        self.choice = None
        self.content = None

        # Labels only (menu display / choice numbering); all modes share one sender.
        self.modes = (
            "Campaign with text",
            "Single-account campaign",
            "Campaign with media",
            "Campaign with reply",
            "Campaign with stickers",
        )

    def _base_content(self):
        """Base (text, entities, media) for one send.

        Bot path: the operator's captured RichContent. CLI path: a random config
        message parsed as HTML (preserving config formatting / <emoji> tags).
        """
        if self.content is not None:
            return self.content.text, self.content.entities, self.content.media

        text, entities = html.parse(random.choice(self.settings.messages))
        return text, entities, []

    def _append_mentions(self, text, entities, user_ids):
        text = text or ""
        entities = list(entities)

        first = True
        for user_id in user_ids:
            if not first:
                text += _MENTION_CHARS  # unlinked separator
            first = False

            offset = _utf16_len(text)
            text += _MENTION_CHARS
            entities.append(types.MessageEntityMentionName(offset, 2, user_id=user_id))

        return text, entities

    async def _send(self, session, peer, content, report, reply_to=None):
        kwargs = {}
        if self.choice == 3 and reply_to:  # reply mode
            kwargs["reply_to"] = reply_to

        if self.choice == 4 and getattr(self, "sticker_set", None):  # stickers first
            stickers = await session(
                GetStickerSetRequest(
                    stickerset=InputStickerSetShortName(short_name=self.sticker_set)
                )
            )
            await self.safe_call(lambda: session.send_file(peer, random.choice(stickers.documents)))

        await rich_message.send(session, peer, content, self.safe_call, report=report, **kwargs)

    def configure(self, choice, mention_all=False, mention_mode=None, sticker_set=None, delay=None, content=None):
        """Set campaign parameters without prompting (used by the bot)."""
        self.choice = choice
        self.content = content
        self.mention_all = mention_all
        self.mention_mode = mention_mode

        if sticker_set:
            self.sticker_set = sticker_set.replace("https://t.me/addstickers/", "")

        self.delay_range = delay or self.settings.delay

    async def broadcast(self, session, peer, report, reply_to=None):
        users_ids = []
        admin_ids = []

        count = 0
        errors = 0

        try:
            me = await session.get_me()
        except Exception as err:
            await report(f"get_me failed: {err}")
            return

        if self.mention_all:
            try:
                admins = await session.get_participants(
                    peer,
                    filter=types.ChannelParticipantsAdmins
                )
                admin_ids = [user.id for user in admins]

                if self.mention_mode == "users":
                    users_ids = [
                        user.id for user in await session.get_participants(peer)
                        if user.id not in admin_ids
                    ]
            except Exception as err:
                await report(f"[{me.first_name}] can't read participants, sending without mentions: {err}")

        while count < self.settings.messages_count \
                or self.settings.messages_count == 0:
            text, entities, media = self._base_content()

            if self.mention_all:
                sample = (
                    random.sample(users_ids, min(len(users_ids), 10))
                    if self.mention_mode == "users"
                    else random.sample(admin_ids, min(len(admin_ids), 2))
                )
                text, entities = self._append_mentions(text, entities, sample)

            content = RichContent(text=text, entities=entities, media=media)

            try:
                await self._send(session, peer, content, report, reply_to=reply_to)
            except AccountLimited as err:
                await report(f"[{me.first_name}] limit, stopping. {err}")
                break
            except Exception as err:
                await report(f"[{me.first_name}] not sent. {err}")

                errors += 1

                if errors >= 3:
                    try:
                        await session.delete_dialog(peer)
                    except Exception as err:
                        await report(f"ERROR while leaving from chat: {err}")

                    break

            else:
                count += 1
                await report(f"[{me.first_name}] sent. COUNT: {count}")

            # delay between sends only; a break (limit / 3 errors) skips it
            await self.delay()

    async def handle(self, session, report):
        async def handler(message: types.Message):
            if message.raw_text == self.settings.trigger:
                # local, not shared state: concurrent per-worker listeners must not
                # clobber each other's reply target
                reply_to = message.reply_to.reply_to_msg_id if message.reply_to else None

                await self.broadcast(
                    session,
                    message.chat_id,
                    report,
                    reply_to=reply_to,
                )

        session.add_event_handler(handler, events.NewMessage)

        try:
            if not self.storage.initialize:
                await session.connect()

            await session.run_until_disconnected()
        finally:
            session.remove_event_handler(handler, events.NewMessage)

    def ask(self):
        for index, label in enumerate(self.modes):
            console.print(
                "[bold white][{index}] {description}[/]"
                .format(index=index + 1, description=label),
            )

        choice = console.input(
            "[bold white]>> [/]"
        )

        while not (choice.isdigit() and 1 <= int(choice) <= len(self.modes)):
            choice = console.input(
                "[bold white]>> [/]"
            )

        self.choice = int(choice) - 1
        self.ask_accounts_count()

        if self.choice == 4:
            self.sticker_set = console.input("[bold red]enter link to sticker set (e.g https://t.me/addstickers/AlbinoEmoji)> [/]")
            self.sticker_set = self.sticker_set.replace("https://t.me/addstickers/", "")

        delay = Prompt.ask(
            "[bold red]delay[/]",
            default="-".join(str(x) for x in self.settings.delay)
        )

        self.delay_range = self.parse_delay(delay)
        self.mention_all = Confirm.ask("[bold red]mention all?[/]", default=True)

        if self.mention_all:
            self.mention_mode = Prompt.ask(
                "[bold red]mention mode[/]",
                choices=["admins", "users"]
            )

        return self.choice

    async def start_single_campaign(self, sessions, link, report):
        for session in sessions:
            async with self.storage.ainitialize_session(session):
                await self.broadcast(
                    session,
                    link,
                    report,
                )

    async def start_campaign(self, report):
        if self.choice == 1:
            link = Prompt.ask("[bold red]link to chat[/]")

            await self.start_single_campaign(self.sessions, link, report)
            return

        await report('[*] Send "{trigger}" to chat'.format(trigger=self.settings.trigger))

        await asyncio.gather(*[
            self.handle(session, report)
            for session in self.sessions
        ])
