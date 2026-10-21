"""📁 Проекты: the list and new projects (listing.py), a project's card's buttons (card.py), the project's own
settings (manage.py); how they are shown: views.py."""
from aiogram import Router

from bot.routers.projects import card, listing, manage, views

__all__ = ["card", "listing", "manage", "router", "views"]

router = Router()
router.include_routers(listing.router, card.router, manage.router)
