import random

from telethon.tl.functions.account import UpdateProfileRequest
from modules.console import console
from functions.base import TelethonFunction
from functions.base.base import console_report


class ChangeBioFunc(TelethonFunction):
    """Change bio"""

    async def change_bio(self, session, report, bio=None, bios=None):
        if bios is not None:
            bio = random.choice(bios)
        await self.request_each(session, report, UpdateProfileRequest(about=bio), "bio changed", "not changed")

    async def run(self, report, bio=None, bios=None):
        await self.run_sequential(
            lambda session, report: self.change_bio(session, report, bio=bio, bios=bios),
            report,
        )

    async def execute(self):
        self.ask_accounts_count()

        bio = console.input("[bold red]bio> [/]")

        await self.run(console_report, bio=bio)
