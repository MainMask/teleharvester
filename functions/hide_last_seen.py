
from telethon.tl.functions.account import SetPrivacyRequest
from telethon.tl.types import InputPrivacyKeyStatusTimestamp, InputPrivacyValueDisallowAll

from functions.base import TelethonFunction

HIDE = SetPrivacyRequest(InputPrivacyKeyStatusTimestamp(), [InputPrivacyValueDisallowAll()])  # the warmup's too


class HideLastSeenFunc(TelethonFunction):
    """Hide last seen"""

    async def hide(self, session, report):
        if await self.request_each(
                session, report, HIDE,
                "последний визит скрыт", "не удалось скрыть"):
            self.mark_done(session)

    async def run(self, report):
        await self.run_sequential(self.hide, report)
