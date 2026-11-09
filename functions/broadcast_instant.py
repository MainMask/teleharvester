import asyncio
from functions.base import TelethonFunction
from functions.broadcast import Broadcast


class InstantBroadcastFunc(TelethonFunction):
    """Instant broadcast (no trigger)"""

    async def _one(self, broadcast, session, link, report):
        async with self.storage.ainitialize_session(session):
            await broadcast.broadcast(session, link, report)

    async def run(self, choice, mention_all, mention_mode, sticker_set, content, link, report):
        broadcast = Broadcast(self.storage, self.settings)
        broadcast.configure(choice, mention_all, mention_mode, sticker_set, content)
        broadcast.sessions = list(self.sessions)
        broadcast.progress = self.progress
        per_worker = broadcast.messages_count  # 0: unlimited, a counter only
        self.progress_total(len(self.sessions) * per_worker if per_worker else None)

        await asyncio.gather(*[
            self._one(broadcast, session, link, report)
            for session in self.sessions
        ])
