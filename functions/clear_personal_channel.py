
from telethon.tl.functions.account import UpdatePersonalChannelRequest
from telethon.tl.types import InputChannelEmpty

from functions.base import TelethonFunction


class ClearPersonalChannelFunc(TelethonFunction):
    """Clear personal channel"""

    async def clear(self, session, report):
        if await self.request_each(session, report, lambda: UpdatePersonalChannelRequest(InputChannelEmpty()),
                                   "канал убран из профиля", "не удалось убрать канал"):
            self.mark_done(session)

    async def run(self, report):
        await self.run_sequential(self.clear, report)
