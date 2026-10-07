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
            sys.exit("Токен бота переехал в .env: перенесите [bot].token в BOT_TOKEN и удалите его из config.toml.")

        token = os.environ.get("BOT_TOKEN", "")
        admins = bot.get("admins", [])

        env_admins = os.environ.get("BOT_ADMINS")
        if env_admins:
            admins = env_admins.replace(",", " ").split()

        self.token: str = token
        try:
            self.admins: set[int] = {int(a) for a in admins}
        except (TypeError, ValueError):
            sys.exit(f"Админы бота должны быть числовыми Telegram ID, а не {admins!r} "
                     "(config.toml [bot].admins / BOT_ADMINS).")

    def validate(self):
        if not self.token:
            sys.exit("BOT_TOKEN пуст (укажите его в .env). Получить токен — у @BotFather.")
        if not self.admins:
            sys.exit("config.toml [bot].admins пуст (или задайте BOT_ADMINS). Добавьте свой Telegram ID.")
