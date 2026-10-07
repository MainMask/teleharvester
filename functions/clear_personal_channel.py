
from telethon.tl.functions.account import UpdatePersonalChannelRequest
from telethon.tl.types import InputChannelEmpty

from functions.base import TelethonFunction
from functions.base.base import console_report


class ClearPersonalChannelFunc(TelethonFunction):
    """Clear personal channel"""

    async def clear(self, session, report):
        if await self.request_each(session, report, UpdatePersonalChannelRequest(InputChannelEmpty()),
                                   "канал убран из профиля", "не удалось убрать канал"):
            self.mark_done(session)

    async def run(self, report):
        await self.run_sequential(self.clear, report)

    async def execute(self):
        self.ask_workers()

        await self.run(console_report)
