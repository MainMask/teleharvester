from functions.base import TelethonFunction
from functions.base.base import console_report
from functions.broadcast import Broadcast, ask_run_options, save_run_options


class BroadcastChatFunc(TelethonFunction):
    """Broadcast to chat"""

    def prepare(self, choice, mention_all, mention_mode, sticker_set, content):
        """Build a configured Broadcast over the delegated workers (for the bot)."""
        broadcast = Broadcast(self.storage, self.settings)
        broadcast.configure(choice, mention_all, mention_mode, sticker_set, content)
        broadcast.sessions = list(self.sessions)
        broadcast.progress = self.progress
        self.progress_total(None)  # a listener: how many triggers will come is unknown
        return broadcast

    def listener_coros(self, broadcast, report):
        """Trigger-listener coroutine per worker; started as background tasks by the bot."""
        return [
            broadcast.handle(session, report)
            for session in broadcast.sessions
        ]

    async def execute(self):
        broadcast = Broadcast(self.storage, self.settings)

        broadcast.ask()
        options = ask_run_options(self.settings, trigger=True)
        if not save_run_options(self.settings, options):
            return
        await broadcast.start_campaign(console_report)
