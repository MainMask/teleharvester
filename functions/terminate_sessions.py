
from modules.console import console

from telethon.tl.functions.account import GetAuthorizationsRequest, ResetAuthorizationRequest
from telethon import TelegramClient

from functions.base import TelethonFunction
from functions.base.base import console_report


class TerminateSessionsFunc(TelethonFunction):
    """Terminate other authorized sessions"""

    async def terminate_sessions(self, session: TelegramClient, report):
        async with self.storage.ainitialize_session(session):
            try:
                authorizations = await session(GetAuthorizationsRequest())
            except Exception as error:
                self.progress_failed()
                await report(f"[!] не удалось получить список сессий: {error}")
                return

            failed = False
            for authorization in authorizations.authorizations:
                if authorization.hash != 0:
                    try:
                        await session(ResetAuthorizationRequest(hash=authorization.hash))
                    except Exception as error:
                        failed = True
                        self.progress_failed()
                        await report(f"[!] ошибка: {error}")
                    else:
                        self.progress_ok()
                        await report(f"Сессия сброшена: {authorization.ip} ({authorization.device_model}, {authorization.platform})")
            if not failed:
                self.mark_done(session)

    async def run(self, report):
        await self.gather_in_order(self.terminate_sessions, report)

    async def execute(self):
        self.ask_workers()

        with console.status("Сброс сессий..."):
            await self.run(console_report)
