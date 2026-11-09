"""🤖 Аккаунты: the project's workers and the personal accounts — the screen with the accounts by category (base.py),
the workers' poll (workers.py), each account's card (card.py) —, workers moved by ticks (move.py), the
proxies (proxies.py), workers from files — tdata, .session — (upload.py)."""
from aiogram import Router

from bot.routers.accounts import base, card, move, proxies, upload, workers

__all__ = ["base", "card", "move", "proxies", "router", "upload", "workers"]

router = Router()
router.include_routers(card.router, move.router, proxies.router, upload.router, base.router)
