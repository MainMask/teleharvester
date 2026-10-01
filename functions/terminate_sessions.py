import asyncio

from rich.progress import track
from rich.console import Console

from telethon.tl.functions.account import GetAuthorizationsRequest, ResetAuthorizationRequest
from telethon import TelegramClient

from functions.base import TelethonFunction
from functions.base.base import console_report

console = Console()


class TerminateSessionsFunc(TelethonFunction):
    """Terminate other authorized sessions"""

    async def terminate_sessions(self, session: TelegramClient, report):
        async with self.storage.ainitialize_session(session):
            try:
                authorizations = await session(GetAuthorizationsRequest())
            except Exception as error:
                await report(f"Error while getting authorizations : {error}")
                return

            for authorization in authorizations.authorizations:
                if authorization.hash != 0:
                    try:
                        await session(ResetAuthorizationRequest(hash=authorization.hash))
                    except Exception as error:
                        await report(f"Error : {error}")
                    else:
                        await report(f"Reset authorization {authorization.ip} ({authorization.device_model}, {authorization.platform})")

    async def run(self, report):
        await asyncio.gather(*[
            self.terminate_sessions(session, report)
            for session in self.sessions
        ])

    async def execute(self):
        self.ask_accounts_count()

        await asyncio.gather(*[
            self.terminate_sessions(session, console_report)
            for session in track(self.sessions, "Terminating...")
        ])
