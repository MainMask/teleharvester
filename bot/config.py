import os
import sys

import toml


class BotConfig:
    """Control-panel bot settings: token + admin whitelist.

    Read from config.toml's [bot] table; env BOT_TOKEN / BOT_ADMINS override.
    """

    def __init__(self, path: str = "config.toml"):
        token = ""
        admins = []

        if os.path.exists(path):
            with open(path) as file:
                bot = toml.load(file).get("bot", {})

            token = bot.get("token", "")
            admins = bot.get("admins", [])

        token = os.environ.get("BOT_TOKEN", token)

        env_admins = os.environ.get("BOT_ADMINS")
        if env_admins:
            admins = env_admins.replace(",", " ").split()

        self.token: str = token
        self.admins: set[int] = {int(a) for a in admins}

    def validate(self):
        if not self.token:
            sys.exit("config.toml [bot].token is empty (or set BOT_TOKEN). Get one from @BotFather.")
        if not self.admins:
            sys.exit("config.toml [bot].admins is empty (or set BOT_ADMINS). Add your Telegram user ID.")
