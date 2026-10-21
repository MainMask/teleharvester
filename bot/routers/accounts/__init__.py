"""🤖 Аккаунты: the project's workers and the personal accounts — the list (workers.py), each account's card
(card.py), workers moved by ticks (move.py), the proxies (proxies.py), a worker from tdata (tdata.py)."""
from aiogram import Router

from bot.routers.accounts import card, move, proxies, tdata, workers

__all__ = ["card", "move", "proxies", "router", "tdata", "workers"]

router = Router()
router.include_routers(workers.router, card.router, move.router, proxies.router, tdata.router)
