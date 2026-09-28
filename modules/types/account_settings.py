from dataclasses import dataclass

from modules.types.account import Account
from modules.types.application import Application
from modules.types.proxy import Proxy


@dataclass
class AccountSettings:
    auth_key: str

    account: Account
    application: Application
    proxy: Proxy | None

    @staticmethod
    def from_dict(session_dict: dict) -> "AccountSettings":
        proxy = session_dict.get("proxy")
        
        return AccountSettings(
            auth_key=session_dict["auth_key"],
            account=Account(**session_dict["account"]),
            application=Application(**session_dict["application"]),
            proxy=Proxy(**proxy) if proxy else None
        )
