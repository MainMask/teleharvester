
from telethon.tl.functions.account import SetPrivacyRequest
from telethon.tl.types import InputPrivacyKeyStatusTimestamp, InputPrivacyValueDisallowAll

from functions.base import TelethonFunction
from functions.base.base import console_report


class HideLastSeenFunc(TelethonFunction):
    """Hide last seen"""

    async def hide(self, session, report):
        await self.request_each(
            session, report, SetPrivacyRequest(InputPrivacyKeyStatusTimestamp(), [InputPrivacyValueDisallowAll()]),
            "last seen hidden", "not hidden")

    async def run(self, report):
        await self.run_sequential(self.hide, report)

    async def execute(self):
        self.ask_accounts_count()

        await self.run(console_report)
