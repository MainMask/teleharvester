import asyncio

from bot.app import run_bot

if __name__ == "__main__":
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        pass
    # SystemExit is left to propagate so config error messages (BotConfig.validate,
    # Settings) reach the user instead of being swallowed.
