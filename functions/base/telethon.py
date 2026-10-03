import random

from functions.base.base import BaseFunction
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
