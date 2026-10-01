from functions.base import TelethonFunction
from functions.base.base import console_report
from functions.broadcast import Broadcast


class BroadcastChatFunc(TelethonFunction):
    """Broadcast to chat"""

    def prepare(self, choice, mention_all, mention_mode, sticker_set):
        """Build a configured Broadcast over the delegated workers (for the bot)."""
        broadcast = Broadcast(self.storage, self.settings)
        broadcast.configure(choice, mention_all, mention_mode, sticker_set, self.settings.delay)
        broadcast.sessions = list(self.sessions)
        return broadcast

    def listener_coros(self, broadcast, report):
        """Trigger-listener coroutine per worker; started as background tasks by the bot."""
        return [
            broadcast.handle(session, broadcast.function, report)
            for session in broadcast.sessions
        ]

    async def execute(self):
        broadcast = Broadcast(self.storage, self.settings)

        broadcast.ask()
        await broadcast.start_campaign(console_report)
