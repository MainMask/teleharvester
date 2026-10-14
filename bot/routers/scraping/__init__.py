"""📁 Проекты' scraper work in the bot: a scrape and top-up, a group's members, a verify, the analysis of
the files — each in its module, what they share in common.py."""
from aiogram import Router

from bot.routers.scraping import analysis, common, members, scrape, verify
from bot.routers.scraping.analysis import analysis_project
from bot.routers.scraping.members import members_project
from bot.routers.scraping.scrape import resume_after_restart, scrape_project, stop_for_shutdown, topup_project
from bot.routers.scraping.verify import verify_project

__all__ = ["analysis_project", "members_project", "resume_after_restart", "router", "scrape_project",
           "stop_for_shutdown", "topup_project", "verify_project"]

router = Router()
router.include_routers(common.router, scrape.router, members.router, verify.router, analysis.router)
