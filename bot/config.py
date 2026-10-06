import os
import sys

from modules.config import load_toml


class BotConfig:
    """Control-panel bot settings: token + admin whitelist.

    The token is a secret and comes only from env BOT_TOKEN (.env); admins come
    from config.toml's [bot] table, overridden by env BOT_ADMINS.
    """

    def __init__(self, path: str = "config.toml"):
        bot = load_toml(path).get("bot", {})

        if bot.get("token"):  # the old example's empty `token = ""` is harmless
            sys.exit("Bot token moved to .env: put [bot].token into BOT_TOKEN and delete it from config.toml.")

        token = os.environ.get("BOT_TOKEN", "")
        admins = bot.get("admins", [])

        env_admins = os.environ.get("BOT_ADMINS")
        if env_admins:
            admins = env_admins.replace(",", " ").split()

        self.token: str = token
        try:
            self.admins: set[int] = {int(a) for a in admins}
        except (TypeError, ValueError):
            sys.exit(f"Bot admins must be numeric Telegram user IDs, not {admins!r} "
                     "(config.toml [bot].admins / BOT_ADMINS).")

    def validate(self):
        if not self.token:
            sys.exit("BOT_TOKEN is empty (set it in .env). Get one from @BotFather.")
        if not self.admins:
            sys.exit("config.toml [bot].admins is empty (or set BOT_ADMINS). Add your Telegram user ID.")
