import random
from abc import ABC, abstractmethod


class Application(ABC):
    api_id: int
    api_hash: str
    
    lang_pack: str
    
    @staticmethod
    def system_lang_code():
        return random.choice(["zh-hans", "en", "ru", "af", "sq", "cs", "pl"])
    
    @staticmethod
    @abstractmethod
    def app_version() -> str:
        raise NotImplementedError()

    @staticmethod
    @abstractmethod
    def device() -> str:
        raise NotImplementedError()
    
    @staticmethod
    @abstractmethod
    def sdk() -> str:
        raise NotImplementedError()
