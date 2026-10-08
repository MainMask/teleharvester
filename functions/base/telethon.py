import random

from rich.prompt import Prompt

from functions.base.base import AccountLimited, BaseFunction, _report_console
from modules import profile_done, restricted_workers
from modules.scraper_creds import scrape_accounts
from modules.storages.sessions_storage import SessionsStorage
from modules.settings import Settings

from typing import List
from telethon import functions, types
from telethon.sync import TelegramClient


class TelethonFunction(BaseFunction):
    def __init__(self, storage: SessionsStorage, settings: Settings):
        self.storage = storage
        self.settings = settings

        self.sessions: List[TelegramClient] = storage.sessions

    async def get_me(self, session):
        """session.get_me(), raising for a banned / logged-out account (Telethon returns None for it),
        so the caller's get_me error branch skips the worker instead of crashing on me.first_name."""
        me = await session.get_me()
        if me is None:
            raise AccountLimited("сессия мертва (бан или выход)")
        return me

    async def resolve_chat(self, session, peer):
        """A private link's chat (PeerChannel: no username to look up) resolves only from the session's
        cache, empty in a fresh process and after each release: on a miss the dialogs are loaded once,
        as the scraper's _warm_channel does. Any other peer is returned as is."""
        if not isinstance(peer, types.PeerChannel):
            return peer
        try:
            return await session.get_input_entity(peer)
        except ValueError:
            await session.get_dialogs()  # their entities land in the cache
            return await session.get_input_entity(peer)

    async def resolve_message(self, session, peer, message_id, comment=None):
        """(chat, message id) to act on: the post itself, or with `comment` (a …?comment=<id>
        link, see comment_id) that comment, which lives in the channel's discussion group."""
        peer = await self.resolve_chat(session, peer)
        if comment is None:
            return peer, message_id
        full = await session(functions.channels.GetFullChannelRequest(peer))
        linked = full.full_chat.linked_chat_id
        chat = next((c for c in full.chats if c.id == linked), None) if linked else None
        if chat is None:
            raise ValueError("у канала нет чата обсуждения")
        return chat, comment

    async def request_each(self, session, report, request, done: str, failed: str) -> bool:
        """One request on one worker, reported under its name: `done`, or `failed: <error>`;
        True if it went through."""
        async with self.storage.ainitialize_session(session):
            try:
                me = await self.get_me(session)
            except Exception as err:
                self.progress_failed()
                await report(f"не удалось опросить аккаунт: {err}")
                return False

            try:
                await session(request)
            except Exception as err:
                self.progress_failed()
                await report(f"[{me.first_name}] {failed}: {err}")
                return False
            self.progress_ok()
            await report(f"[{me.first_name}] {done}")
            return True

    def mark_done(self, session, value=None):
        """Note that this function has changed the worker (to `value`, if picked from a list;
        see modules.profile_done)."""
        key = profile_done.worker_key(self.storage, self.storage.get_session_path(session))
        profile_done.mark(key, type(self).__name__, value)

    @staticmethod
    def parse_picks(raw: str, total: int, new: list[int]) -> list[int]:
        """Worker indexes from the CLI's answer: "" or "new" the new ones, "all" every one,
        else numbers and ranges from 1 ("1,3,5-8"); [] for an answer that is none of these."""
        raw = raw.strip().lower()
        if raw in ("", "new"):
            return new
        if raw == "all":
            return list(range(total))
        picked = set()
        for part in raw.replace(" ", "").split(","):
            first, _, last = part.partition("-")
            if not first.isdigit() or (last and not last.isdigit()):
                return []
            first, last = int(first), int(last or first)
            if not 1 <= first <= last <= total:
                return []
            picked.update(range(first - 1, last))
        return sorted(picked)

    def ask_workers(self):
        """CLI: which workers to run on; the ones this function has not changed yet by default."""
        self.sessions = self.storage.sessions  # reset to full list (instance is reused across runs)
        self.on_hold = []
        if not self.sessions:
            return  # nothing to choose from; functions guard the empty case themselves

        accounts = scrape_accounts(self.storage)
        keys = [profile_done.worker_key(self.storage, account.path) for account in accounts]
        profile_done.seed(keys)
        ledger, classname = profile_done.load(), type(self).__name__
        forever, status = set(restricted_workers.load()), restricted_workers.load_status()

        for index, (account, key) in enumerate(zip(accounts, keys), 1):
            notes = profile_done.notes(key, account.path, classname, ledger, forever, status)
            _report_console.print(f"  [{index}] {' '.join([account.label, *notes])}", markup=False, highlight=False)
        new = [i for i, key in enumerate(keys) if classname not in ledger["done"].get(key, {})]
        _report_console.print(f"[bold white]✓ — уже менялось этой функцией; новых: {len(new)}[/]")

        while True:
            raw = Prompt.ask("[bold magenta]номера (1,3,5-8), all — все, Enter — только новые[/]", default="")
            picked = self.parse_picks(raw, len(accounts), new)
            if picked:
                break
        self.sessions = [accounts[i].client for i in picked]

    async def import_phone_contact(self, session, phone):
        """Resolve a phone number to users via ImportContactsRequest.

        Returns the resolved users list (empty if Telegram found no account).
        """
        result = await self.safe_call(lambda: session(functions.contacts.ImportContactsRequest(
            contacts=[types.InputPhoneContact(
                client_id=random.randrange(-2**63, 2**63),
                phone=phone,
                first_name='contact',
                last_name=''
            )]
        )))

        return result.users
