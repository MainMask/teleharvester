
from telethon.tl.functions.account import UpdatePersonalChannelRequest
from telethon.tl.types import InputChannelEmpty

from functions.base import TelethonFunction
from functions.base.base import console_report


class ClearPersonalChannelFunc(TelethonFunction):
    """Clear personal channel"""

    async def clear(self, session, report):
        await self.request_each(session, report, UpdatePersonalChannelRequest(InputChannelEmpty()),
                                "personal channel cleared", "not cleared")

    async def run(self, report):
        await self.gather_in_order(self.clear, report)

    async def execute(self):
        self.ask_accounts_count()

        await self.run(console_report)
