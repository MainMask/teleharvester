import dataclasses
import datetime
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

    @staticmethod
    def from_client(client, me, proxy: Proxy | None, password: str | None, lang_pack: str) -> "AccountSettings":
        """The settings of a freshly authorized client: its key, its account and the device
        and app it presented (a later load must keep presenting them)."""
        init = client._init_request
        return AccountSettings(
            auth_key=client.session.save(),
            account=Account(
                first_name=me.first_name,
                last_name=me.last_name,
                user_id=me.id,
                added_at=datetime.datetime.now().timestamp(),
                phone_number=me.phone,
                username=me.username,
            ),
            application=Application(
                api_id=client.api_id,
                api_hash=client.api_hash,
                device_name=init.device_model,
                app_version=init.app_version,
                sdk=init.system_version,
                lang_pack=lang_pack,
                system_lang_code=init.system_lang_code,
            ),
            proxy=proxy,
            password=password,
        )

    def save(self, path: str):
        """Write these settings to a `.jsession` file at `path`.

        Via a temp file + rename: the file holds the account's only auth key, so a
        write that dies midway must not leave it truncated."""
        tmp_path = path + ".tmp"
        # 0o600 from creation: the file holds the auth key and the 2FA password
        with os.fdopen(os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fileobj:
            json.dump(dataclasses.asdict(self), fileobj, ensure_ascii=True, indent=4)
            fileobj.flush()
            os.fsync(fileobj.fileno())  # on disk before the rename: a power cut must not lose the key

        if os.path.exists(path):  # the rename must not widen a locked-down file's mode
            shutil.copymode(path, tmp_path)
        os.replace(tmp_path, path)
