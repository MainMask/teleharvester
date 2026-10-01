"""One-time interactive authorisation: save a Telethon session."""

import asyncio

from telethon import TelegramClient
from telethon.sessions import StringSession

from scraper.config import Credentials, session_for, start_kwargs


async def _login(creds: Credentials, session: str, as_string: bool) -> None:
    client = TelegramClient(StringSession() if as_string else session_for(creds, session),
                            creds.api_id, creds.api_hash)
    await client.start(**start_kwargs(creds))
    try:
        me = await client.get_me()
        handle = f" (@{me.username})" if me.username else ""
        print(f"Logged in as {me.first_name}{handle}, id {me.id}")
        if isinstance(client.session, StringSession):
            print("Add this line to .env (it gives full access to the account - keep it private):")
            print(f"TG_SESSION_STRING={client.session.save()}")
        else:
            print(f"Session saved: {session if session.endswith('.session') else session + '.session'}")
    finally:
        await client.disconnect()


def login(creds: Credentials, session: str, as_string: bool = False) -> None:
    asyncio.run(_login(creds, session, as_string))
