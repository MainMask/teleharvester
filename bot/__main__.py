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

    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        pass
    # SystemExit is left to propagate so config error messages (BotConfig.validate,
    # Settings) reach the user instead of being swallowed.
