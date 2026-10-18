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

    async def cached_me(self, session):
        """get_me() once per worker in a run (self._me_cache, which the run resets); AccountLimited if it
        can't be polled, so a rotation moves on to the next worker."""
        if id(session) not in self._me_cache:
            try:
                self._me_cache[id(session)] = await self.get_me(session)
            except AccountLimited:
                raise
            except Exception as err:
                raise AccountLimited(f"не удалось опросить аккаунт: {err}")
        return self._me_cache[id(session)]

    async def poll_me(self, session, report):
        """get_me() for a worker's run; None once it is reported as failed (an error in the job's
        summary): the caller skips the worker, the others still run."""
        try:
            return await self.get_me(session)
        except Exception as err:
            self.progress_failed()
            await report(f"не удалось опросить аккаунт: {err}")
            return None

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
            me = await self.poll_me(session, report)
            if me is None:
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
            _report_console.print("[bold red]Никого не выбрано: номера из списка или all — все.[/]")
        self.sessions = [accounts[i].client for i in picked]

    async def reconnect_rebuilt(self, old: dict):
        """After a proxy change (`old`: full_sessions before it): the CLI keeps clients connected, so
        drop the ones a new client replaced and connect the rebuilt ones."""
        for path, client in old.items():
            if self.storage.full_sessions.get(path) is not client:
                await client.disconnect()
                if self.storage.initialize:
                    await self.storage.check_session(self.storage.full_sessions[path], path)

    async def by_username(self, session, row):
        """A base row's peer by its username, if the username is still that person's: an old base's
        username may have been taken by someone else since, who must not be written to or added."""
        peer = await self.safe_call(lambda: session.get_input_entity(row["username"]))
        if getattr(peer, "user_id", None) != row["user_id"]:
            raise ValueError(f"@{row['username']} теперь у другого аккаунта")
        return peer

    async def import_phone_contact(self, session, phone):
        """Resolve a phone number to users via ImportContactsRequest.

        Returns the resolved users list (empty if Telegram found no account). AccountLimited when
        the worker is out of imports: Telegram then puts the number in retry_contacts, not an error.
        """
        result = await self.safe_call(lambda: session(functions.contacts.ImportContactsRequest(
            contacts=[types.InputPhoneContact(
                client_id=random.randrange(-2**63, 2**63),
                phone=phone,
                first_name='contact',
                last_name=''
            )]
        )))

        if result.retry_contacts:
            raise AccountLimited("лимит импорта контактов")
        return result.users
