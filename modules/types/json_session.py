import random
from datetime import datetime
from typing import Any

from telethon import TelegramClient
from telethon.sessions import StringSession

from modules.generators.linux import LinuxAPI
from modules.generators.telegram_android import TelegramAppAPI
from modules.types.account import Account
from modules.types.account_settings import AccountSettings
from modules.types.application import Application
from modules.types.proxy import Proxy


class JsonSession:
    def __init__(self, *, account_settings=None, dict_settings=None):
        if account_settings is not None:
            self.account: AccountSettings = account_settings

        elif dict_settings is not None:
            self.account: AccountSettings = AccountSettings.from_dict(dict_settings)

    @staticmethod
    async def create_application_session(
        generator: Application | Any = None,
        proxy: Proxy | Any = None,
        api_hash: str | Any = None,
        api_id: str | Any = None,
        device_name: str | Any = None,
        app_version: str | Any = None,
        sdk: str | Any = None,
        password: str | Any = None,
    ):
        if not generator:
            generator = random.choice([LinuxAPI, TelegramAppAPI])

        api_hash = api_hash or generator.api_hash
        api_id = api_id or generator.api_id
        app_version = app_version or generator.app_version()
        device_name = device_name or generator.device()
        sdk = sdk or generator.sdk()
        lang_pack = generator.lang_pack
        system_lang_code = generator.system_lang_code()

        async with TelegramClient(
            session=StringSession(),
            api_id=api_id,
            api_hash=api_hash,
            device_model=device_name,
            app_version=app_version,
            system_version=sdk,
            lang_code=system_lang_code,
            system_lang_code=system_lang_code,
            proxy=proxy.as_telethon() if proxy else None
        ) as client:
            account = await client.get_me()

            account_settings = AccountSettings(
                auth_key=client.session.save(),
                account=Account(
                    first_name=account.first_name,
                    last_name=account.last_name,
                    user_id=account.id,
                    added_at=datetime.now().timestamp(),
                    phone_number=account.phone,
                    username=account.username,
                ),
                application=Application(
                    api_id=api_id,
                    api_hash=api_hash,
                    device_name=device_name,
                    app_version=app_version,
                    sdk=sdk,
                    lang_pack=lang_pack,
                    system_lang_code=system_lang_code,
                ),
                proxy=proxy,
                password=password,
            )

            account_settings.save(f"{account.phone}.jsession")
