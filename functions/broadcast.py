import asyncio
import os
import random
from rich.prompt import Prompt, Confirm
from modules.console import console
from telethon import events, types
from telethon.errors import (
    ChannelPrivateError,
    ChatAdminRequiredError,
    ChatWriteForbiddenError,
)
from telethon.extensions import html
from telethon.tl.functions.messages import GetStickerSetRequest
from telethon.tl.types import InputStickerSetShortName

from functions.base import TelethonFunction
from functions.base.base import AccountLimited
from modules import rich_message
from modules.rich_message import RichContent

# Invisible mention carrier: each hidden mention is these two chars linked to a user.
_MENTION_CHARS = "⁬⁯"

# Every update a listening worker receives adds its users/chats to the session's in-memory
# entity set, which never shrinks; a trigger listener runs for days, so past this many it
# is dropped (the trigger's own chat is in the client's bounded cache from the same update).
LISTENER_ENTITY_LIMIT = 50_000
# A listening worker that loses its connection (a network outage, a dead proxy: Telethon gives
# up after a few quick retries) tries again after this many seconds; the campaign goes on.
LISTENER_RECONNECT_DELAY = 30

# A basic group ignores the admins filter (Telethon returns every member), so the
# participant type decides who is an admin, in groups and supergroups alike.
_ADMIN_PARTICIPANTS = (
    types.ChatParticipantAdmin,
    types.ChatParticipantCreator,
    types.ChannelParticipantAdmin,
    types.ChannelParticipantCreator,
)

# Errors meaning the chat won't accept this account's messages (vs. content/transient ones).
# Not UserBannedInChannelError: that is an account-wide spam restriction (AccountLimited).
_CHAT_ERRORS = (
    ChatWriteForbiddenError,
    ChannelPrivateError,
    ChatAdminRequiredError,
)


def _utf16_len(text: str) -> int:
    """Length in UTF-16 code units (matches Telegram/Telethon entity offsets)."""
    return len(text.encode("utf-16-le")) // 2


class Broadcast(TelethonFunction):
    def __init__(self, storage, settings):
        super().__init__(storage, settings)

        self.choice = None
        self.content = None
        self._stickers = {}  # id(session) -> the sticker set's documents, fetched once per worker

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
            if id(session) not in self._stickers:
                stickers = await self.safe_call(lambda: session(
                    GetStickerSetRequest(
                        stickerset=InputStickerSetShortName(short_name=self.sticker_set),
                        hash=0,
                    )
                ))
                self._stickers[id(session)] = stickers.documents
            documents = self._stickers[id(session)]
            await self.safe_call(lambda: session.send_file(peer, random.choice(documents)))

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
            me = await self.get_me(session)
        except Exception as err:
            await report(f"get_me failed: {err}")
            self.progress_drop(self.settings.messages_count)
            return

        if self.mention_all:
            try:
                admins = await session.get_participants(
                    peer,
                    filter=types.ChannelParticipantsAdmins
                )
                admin_ids = [
                    user.id for user in admins
                    if isinstance(getattr(user, "participant", None), _ADMIN_PARTICIPANTS)
                ]

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
            except _CHAT_ERRORS as err:
                # the chat itself refuses this account: leave it, retrying is pointless
                await report(f"[{me.first_name}] can't write to chat, leaving. {err}")

                try:
                    await session.delete_dialog(peer)
                except Exception as err:
                    await report(f"ERROR while leaving from chat: {err}")

                break
            except Exception as err:
                await report(f"[{me.first_name}] not sent. {err}")

                errors += 1

                if errors >= 3:
                    await report(f"[{me.first_name}] 3 errors in a row, stopping.")
                    break

            else:
                errors = 0
                count += 1
                self.progress_step()
                await report(f"[{me.first_name}] sent. COUNT: {count}")

            # delay between sends only; a break (limit / 3 errors) skips it, as does the last send
            if not (self.settings.messages_count and count >= self.settings.messages_count):
                await self.delay()

        if self.settings.messages_count:  # stopped early: its unsent rest leaves the total
            self.progress_drop(self.settings.messages_count - count)

    async def handle(self, session, report):
        # chats this worker is broadcasting to now: Telethon runs handlers concurrently, so a
        # repeated trigger there would start a second loop and double the rate; it is ignored
        active = set()
        label = os.path.basename(self.storage.get_session_path(session) or "") or "worker"

        async def handler(message: types.Message):
            if len(session.session._entities) > LISTENER_ENTITY_LIMIT:  # workers: StringSession
                session.session._entities.clear()

            # an empty trigger would match every media message without a caption
            if self.settings.trigger and message.raw_text == self.settings.trigger:
                if message.chat_id in active:
                    await report(f"[{label}] уже рассылаю в этот чат — повторный триггер пропущен")
                    return
                active.add(message.chat_id)  # before any await: a concurrent trigger sees it

                # local, not shared state: concurrent per-worker listeners must not
                # clobber each other's reply target
                reply_to = message.reply_to.reply_to_msg_id if message.reply_to else None

                try:
                    await self.broadcast(
                        session,
                        message.chat_id,
                        report,
                        reply_to=reply_to,
                    )
                finally:
                    active.discard(message.chat_id)

        # incoming only: a worker's own broadcast must never trigger it again
        session.add_event_handler(handler, events.NewMessage(incoming=True))

        try:
            while True:
                try:
                    await session.connect()  # a no-op if connected (the CLI's clients are)
                    await session.run_until_disconnected()
                    return  # disconnected on purpose: the job is over
                except OSError as err:  # ConnectionError: Telethon's reconnects ran out
                    await report(f"[!] [{label}] connection lost ({err}), "
                                 f"reconnecting in {LISTENER_RECONNECT_DELAY}s")
                    await asyncio.sleep(LISTENER_RECONNECT_DELAY)
                except Exception as err:  # e.g. a logged-out session: this worker only
                    await report(f"[!] [{label}] listener stopped: {err}")
                    return
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
