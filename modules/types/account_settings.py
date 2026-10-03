import dataclasses
import json
import os
import shutil
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
    password: str | None = None

    @staticmethod
    def from_dict(session_dict: dict) -> "AccountSettings":
        proxy = session_dict.get("proxy")

        return AccountSettings(
            auth_key=session_dict["auth_key"],
            account=Account(**session_dict["account"]),
            application=Application(**session_dict["application"]),
            proxy=Proxy(**proxy) if proxy else None,
            password=session_dict.get("password"),
        )

    def save(self, path: str):
        """Write these settings to a `.jsession` file at `path`.

        Via a temp file + rename: the file holds the account's only auth key, so a
        write that dies midway must not leave it truncated."""
        tmp_path = path + ".tmp"
        with open(tmp_path, "w") as fileobj:
            json.dump(dataclasses.asdict(self), fileobj, ensure_ascii=True, indent=4)

        if os.path.exists(path):  # the rename must not widen a locked-down file's mode
            shutil.copymode(path, tmp_path)
        os.replace(tmp_path, path)
