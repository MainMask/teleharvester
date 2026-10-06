import asyncio
import logging

from bot.app import run_bot

if __name__ == "__main__":
    # INFO so aiogram's polling/retry activity is visible in journald over a long run;
    # the timestamp is redundant under systemd but kept for plain `python -m bot` use.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Telethon logs every worker connect/disconnect at INFO; jobs and the auto-reply's rounds
    # reconnect workers all day, which would flood journald. Its warnings and errors still show.
    logging.getLogger("telethon").setLevel(logging.WARNING)

    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        pass
    # SystemExit is left to propagate so config error messages (BotConfig.validate,
    # Settings) reach the user instead of being swallowed.
