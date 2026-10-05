from telethon.tl.functions.account import UpdateProfileRequest
from modules.console import console
from functions.base import TelethonFunction
from functions.base.base import console_report


class ChangeBioFunc(TelethonFunction):
    """Change bio"""

    async def change_bio(self, session, bio: str, report):
        await self.request_each(session, report, UpdateProfileRequest(about=bio), "bio changed", "not changed")

    async def run(self, bio: str, report):
        await self.gather_in_order(
            lambda session, report: self.change_bio(session, bio, report),
            report,
        )

    async def execute(self):
        self.ask_accounts_count()

        bio = console.input("[bold red]bio> [/]")

        await self.run(bio, console_report)
