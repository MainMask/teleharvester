import asyncio
import json
import os
from contextlib import asynccontextmanager
from typing import Dict, List, Union

from modules.console import console
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

from modules.types.json_session import JsonSession


class SessionsStorage:
    def __init__(self, directory: str, api_id: Union[str, int], api_hash: str, initialize: bool = True):
        self.full_sessions: Dict[str, Union[TelegramClient, JsonSession]] = {}
        self.json_sessions: List[JsonSession] = []
        self.jsessions_paths: Dict[str, JsonSession] = {}

        self.initialize = initialize
        self.loop = None  # the loop clients were connected on (initialize=True only)

        for file in os.listdir(directory):
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

                client = TelegramClient(
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

                self.full_sessions[session_path] = client
                self.json_sessions.append(session)
                self.jsessions_paths[session_path] = session

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

    def _forget_session(self, path: str):
        """Drop a session from every index, so no stale reference survives a removal."""
        self.full_sessions.pop(path, None)
        json_session = self.jsessions_paths.pop(path, None)
        if json_session is not None and json_session in self.json_sessions:
            self.json_sessions.remove(json_session)

    @staticmethod
    async def _drop_client(session):
        """Disconnect a forgotten client, or its keepalive/update tasks run on forever."""
        await session.disconnect()

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

            await self._drop_client(session)  # connect() may have succeeded before the check
            self._forget_session(path)
            return

        except Exception as err:
            console.log(f"Session {path} returned error. {err}. Skipping.")
            await self._drop_client(session)
            self._forget_session(path)
            return

        if not authorized:
            console.log(f"Session {path} is inactive. Moving it to sessions/inactive")
            await self._drop_client(session)
            self._forget_session(path)

            inactive_dir = os.path.join(os.path.dirname(path), "inactive")
            os.makedirs(inactive_dir, exist_ok=True)
            os.rename(path, os.path.join(inactive_dir, os.path.basename(path)))
            return

        console.log(f"Initialized {path}")

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

    @property
    def sessions(self) -> List[TelegramClient]:
        return list(self.full_sessions.values())

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
