import asyncio
from functions.base import TelethonFunction
from functions.broadcast import Broadcast
from rich.console import Console

console = Console()


class InstantBroadcastFunc(TelethonFunction):
    """Instant broadcast (no trigger)"""

    async def execute(self):
        link = console.input("[bold red]link> [/]")
        broadcast = Broadcast(self.storage, self.settings)
        broadcast.ask()

        if not self.storage.initialize:
            for session in broadcast.sessions:
                await session.connect()

        await asyncio.gather(*[
            broadcast.broadcast(session, link, broadcast.function)
            for session in broadcast.sessions
        ])

        if not self.storage.initialize:
            for session in broadcast.sessions:
                await session.disconnect()
