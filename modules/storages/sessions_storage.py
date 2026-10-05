import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from typing import Dict, List, Union

from modules.console import console
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

from modules.types.json_session import JsonSession
from modules.types.proxy import ACCOUNTS_PER_PROXY, Proxy


def natural_key(text: str) -> list:
    """Sort key so that name10 comes after name9."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text)]


def profile_order(me) -> tuple:
    """Sort key for live profiles: by username; accounts without one, then unreachable
    ones (None), keep their order at the end."""
    if me is None:
        return (2, [])
    if not me.username:
        return (1, [])
    return (0, natural_key(me.username))


class SessionsStorage:
    def __init__(self, directory: str, api_id: Union[str, int], api_hash: str, initialize: bool = True):
        self.full_sessions: Dict[str, Union[TelegramClient, JsonSession]] = {}
        self.json_sessions: List[JsonSession] = []
        self.jsessions_paths: Dict[str, JsonSession] = {}
        self.usernames: Dict[str, str] = {}  # path -> username: the pool's order

        self.initialize = initialize
        self.loop = None  # the loop clients were connected on (initialize=True only)

        for file in sorted(os.listdir(directory)):  # stable order (by phone), not filesystem order
            if file.endswith(".session"):
                session_path = os.path.join(directory, file)

                try:
                    with open(session_path) as fileobj:
                        auth_key = fileobj.read().strip()
                except UnicodeDecodeError:  # a binary (SQLite) Telethon session, not a string one
                    continue

                if len(auth_key) not in (353, 369):  # IPv4 / IPv6 DC address
                    continue

                client = TelegramClient(
                    StringSession(auth_key),
                    api_id,
                    api_hash,
                    device_model="Redmi Note 10",
                    lang_code="en",
                    system_lang_code="en",
                )

                self.full_sessions[session_path] = client

            elif file.endswith(".jsession"):
                session_path = os.path.join(directory, file)

                try:
                    with open(session_path) as fileobj:
                        session_settings = json.load(fileobj)

                    session = JsonSession(dict_settings=session_settings)
                except Exception as err:  # one broken file must not stop the CLI / bot start
                    console.print(f"[bold yellow]WARNING:[/] skipped broken session file {session_path}: {err}")
                    continue

                if old_session := self.is_phone_exists(
                    session.account.account.phone_number
                ):
                    old_session_path = self.get_json_session_path(old_session)

                    console.print(
                        f"[bold yellow]WARNING:[/] Same accounts in teleharvester — {old_session_path} matches with {session_path}"
                    )

                    continue

                client = self.build_jsession_client(session)

                self.full_sessions[session_path] = client
                self.json_sessions.append(session)
                self.jsessions_paths[session_path] = session
                if session.account.account.username:
                    self.usernames[session_path] = session.account.account.username

        if self.initialize:
            if len(self.full_sessions) == 0:
                return print(
                    "In order for teleharvester to work, you need to add accounts"
                )

            with console.status("Initializing..."):
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:  # no running loop (CLI path): make one for run_until_complete
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                self.loop = loop

                loop.run_until_complete(
                    asyncio.gather(
                        *[
                            self.check_session(session, path)
                            for path, session in self.full_sessions.items()
                        ]
                    )
                )

    @staticmethod
    def build_jsession_client(session: JsonSession) -> TelegramClient:
        """Create a Telethon client for a `.jsession` from its stored account/app/proxy."""
        return TelegramClient(
            session=StringSession(session.account.auth_key),
            api_id=session.account.application.api_id,
            api_hash=session.account.application.api_hash,
            device_model=session.account.application.device_name,
            app_version=session.account.application.app_version,
            system_version=session.account.application.sdk,
            lang_code=session.account.application.system_lang_code,
            system_lang_code=session.account.application.system_lang_code,
            proxy=session.account.proxy.as_telethon()
            if session.account.proxy else None,
        )

    def add_jsession(self, path: str) -> JsonSession | None:
        """Register a freshly written `.jsession` into the live pool (so an account
        imported at runtime is usable without a restart). Returns the JsonSession, or
        None if its phone is already loaded."""
        with open(path) as fileobj:
            session = JsonSession(dict_settings=json.load(fileobj))

        if self.is_phone_exists(session.account.account.phone_number):
            return None

        self.full_sessions[path] = self.build_jsession_client(session)
        self.json_sessions.append(session)
        self.jsessions_paths[path] = session
        if session.account.account.username:
            self.usernames[path] = session.account.account.username
        return session

    def apply_proxies(self, proxies: List[Proxy]) -> dict:
        """Distribute `proxies` across the `.jsession` accounts (one proxy per
        ACCOUNTS_PER_PROXY accounts), persisting each to its file and rebuilding its
        client so the new proxy takes effect immediately.

        Raises ValueError if there are too few proxies for the ratio. `.session`
        (StringSession) accounts can't store a proxy and are left untouched.
        """
        paths = sorted(self.jsessions_paths)
        required = -(-len(paths) // ACCOUNTS_PER_PROXY)  # ceil division

        if len(proxies) < required:
            raise ValueError(
                f"not enough proxies: {len(proxies)} for {len(paths)} accounts "
                f"({required} needed at {ACCOUNTS_PER_PROXY} accounts per proxy)"
            )

        for index, path in enumerate(paths):
            session = self.jsessions_paths[path]
            session.account.proxy = proxies[index // ACCOUNTS_PER_PROXY]
            session.account.save(path)

            self.full_sessions[path] = self.build_jsession_client(session)

        return {
            "accounts": len(paths),
            "proxies_used": required,
            "string_sessions_skipped": len(self.full_sessions) - len(paths),
        }

    def _forget_session(self, path: str):
        """Drop a session from every index, so no stale reference survives a removal."""
        self.full_sessions.pop(path, None)
        self.usernames.pop(path, None)
        json_session = self.jsessions_paths.pop(path, None)
        if json_session is not None and json_session in self.json_sessions:
            self.json_sessions.remove(json_session)

    async def check_session(self, session: TelegramClient, path: str):
        console.log(f"Initializing session {path}")

        try:
            await session.connect()
            authorized = await session.is_user_authorized()
        except ConnectionError:
            json_session = self.jsessions_paths.get(path)

            if json_session is not None and json_session.account.proxy is not None:
                console.log(
                    f"Error with connection to session {path}. Maybe proxy {json_session.account.proxy.ip} is unreachable?"
                )
            else:
                console.log(f"Error with connection to session {path}")

            # disconnect a forgotten client, or its keepalive/update tasks run on forever;
            # connect() may have succeeded before the check
            await session.disconnect()
            self._forget_session(path)
            return

        except Exception as err:
            console.log(f"Session {path} returned error. {err}. Skipping.")
            await session.disconnect()
            self._forget_session(path)
            return

        if not authorized:
            console.log(f"Session {path} is inactive. Moving it to sessions/inactive")
            await session.disconnect()
            self.move_to_inactive(path)
            return

        console.log(f"Initialized {path}")

    def move_to_inactive(self, path: str):
        """Drop a dead (banned / logged out) session from the pool and move its file
        to sessions/inactive/."""
        self._forget_session(path)

        inactive_dir = os.path.join(os.path.dirname(path), "inactive")
        os.makedirs(inactive_dir, exist_ok=True)
        os.rename(path, os.path.join(inactive_dir, os.path.basename(path)))

    def get_session_path(self, session: TelegramClient | JsonSession) -> str:
        for path, client in self.full_sessions.items():
            if client == session:
                return path

    def get_json_session_path(self, json_session_: TelegramClient | JsonSession) -> str:
        for path, json_session in self.jsessions_paths.items():
            if json_session == json_session_:
                return path

    def is_phone_exists(self, phone: str) -> bool | JsonSession:
        for session in self.json_sessions:
            if session.account.account.phone_number == phone:
                return session

        return False

    def remember_username(self, session: TelegramClient, username: str | None):
        """Note a worker's current username (it orders the pool); a .jsession keeps it."""
        path = self.get_session_path(session)
        if path is None or self.usernames.get(path) == username:
            return

        if username:
            self.usernames[path] = username
        else:
            self.usernames.pop(path, None)

        json_session = self.jsessions_paths.get(path)
        if json_session is not None:
            json_session.account.account.username = username
            json_session.account.save(path)

    def remember_name(self, session: TelegramClient, first_name: str, last_name: str | None):
        """Note a worker's current name; a .jsession keeps it (the account pickers show it)."""
        path = self.get_session_path(session)
        json_session = self.jsessions_paths.get(path)
        if json_session is None:
            return

        account = json_session.account.account
        if (account.first_name, account.last_name) == (first_name, last_name):
            return

        account.first_name, account.last_name = first_name, last_name
        json_session.account.save(path)

    async def fetch_me(self, client: TelegramClient, timeout: float = 20):
        """Live get_me(), or None if the worker can't be polled within `timeout` (it covers
        connect() too: a dead proxy must not stall a whole list)."""
        async def fetch():
            async with self.ainitialize_session(client):
                return await client.get_me()

        try:
            me = await asyncio.wait_for(fetch(), timeout)
        except Exception:
            return None

        if me is not None:
            self.remember_username(client, me.username)  # orders the pool in every job
            self.remember_name(client, me.first_name, me.last_name)
        return me

    @property
    def sessions(self) -> List[TelegramClient]:
        """Workers by username (name10 after name9); ones without a known username
        follow in file order."""
        def order(path):
            username = self.usernames.get(path)
            return (0, natural_key(username)) if username else (1, [])

        return [self.full_sessions[path] for path in sorted(self.full_sessions, key=order)]

    @asynccontextmanager
    async def ainitialize_session(self, session):
        if not self.initialize:
            await session.connect()

        try:
            yield
        finally:
            if not self.initialize:
                await session.disconnect()
                # Bot mode reuses the same worker clients for the whole process life.
                # Telethon keeps appending every RPC result's users/chats to the
                # StringSession's in-memory _entities set, which never shrinks, so drop
                # it when the client is released (the next use re-resolves peers anyway).
                try:
                    session.session._entities.clear()
                except Exception:
                    pass

    def __len__(self):
        return len(self.sessions)
