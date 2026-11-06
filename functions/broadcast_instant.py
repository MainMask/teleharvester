import asyncio
from functions.base import TelethonFunction
from functions.base.base import console_report
from functions.broadcast import Broadcast, ask_run_options, save_run_options
from modules.console import console


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
        per_worker = self.settings.messages_count  # 0: unlimited, a counter only
        self.progress_total(len(self.sessions) * per_worker if per_worker else None)

        await asyncio.gather(*[
            self._one(broadcast, session, link, report)
            for session in self.sessions
        ])

    async def execute(self):
        broadcast = Broadcast(self.storage, self.settings)
        broadcast.ask(modes=(0, 4))  # no trigger: no reply to it
        link = console.input("[bold red]ссылка на чат> [/]")
        if not save_run_options(self.settings, ask_run_options(self.settings)):
            return
        await self.run(broadcast.choice, broadcast.mention_all, broadcast.mention_mode, broadcast.sticker_set,
                       None, link, console_report)
