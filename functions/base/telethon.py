from functions.base.base import BaseFunction
from modules.storages.sessions_storage import SessionsStorage
from modules.settings import Settings

from typing import List
from telethon.sync import TelegramClient

from modules.types.json_session import JsonSession


class TelethonFunction(BaseFunction):
    def __init__(self, storage: SessionsStorage, settings: Settings):
        self.storage = storage
        self.settings = settings
        
        self.sessions: List[TelegramClient] = storage.sessions
