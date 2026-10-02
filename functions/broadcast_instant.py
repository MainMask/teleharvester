import asyncio
from functions.base import TelethonFunction
from functions.base.base import console_report
from functions.broadcast import Broadcast
from rich.console import Console

console = Console()


class InstantBroadcastFunc(TelethonFunction):
    """Instant broadcast (no trigger)"""

    async def _one(self, broadcast, session, link, report):
        async with self.storage.ainitialize_session(session):
            await broadcast.broadcast(session, link, report)

    async def run(self, choice, mention_all, mention_mode, sticker_set, content, link, report):
        broadcast = Broadcast(self.storage, self.settings)
        broadcast.configure(choice, mention_all, mention_mode, sticker_set, self.settings.delay, content)
        broadcast.sessions = list(self.sessions)

        await asyncio.gather(*[
            self._one(broadcast, session, link, report)
            for session in self.sessions
        ])

    async def execute(self):
        link = console.input("[bold red]link> [/]")
        broadcast = Broadcast(self.storage, self.settings)
        broadcast.ask()

        if not self.storage.initialize:
            for session in broadcast.sessions:
                await session.connect()

        await asyncio.gather(*[
            broadcast.broadcast(session, link, console_report)
            for session in broadcast.sessions
        ])

        if not self.storage.initialize:
            for session in broadcast.sessions:
                await session.disconnect()
