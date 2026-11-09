
from telethon import types, functions
from functions.base import TelethonFunction


class ReportUserFunc(TelethonFunction):
    """Moderation report (user)"""

    def __init__(self, storage, settings):
        super().__init__(storage, settings)

        self.reasons = (
            ("🚸 Насилие над детьми", types.InputReportReasonChildAbuse()),
            ("©️ Авторские права", types.InputReportReasonCopyright()),
            ("🎭 Фейковый канал/аккаунт", types.InputReportReasonFake()),
            ("🔞 Порнография", types.InputReportReasonPornography()),
            ("📢 Спам", types.InputReportReasonSpam()),
            ("👊 Насилие", types.InputReportReasonViolence()),
            ("❓ Другое", types.InputReportReasonOther())
        )

    async def report_one(self, session, link, reason_type, comment, report):
        async with self.storage.ainitialize_session(session):
            me = await self.poll_me(session, report)
            if me is None:
                return
            try:
                await session(
                    functions.account.ReportPeerRequest(
                        peer=link,
                        reason=reason_type,
                        message=comment
                    )
                )
            except Exception as err:
                self.progress_failed()
                await report(f"[{me.first_name}] ошибка: {err}")
            else:
                self.progress_ok()
                await report(f"[{me.first_name}] жалоба отправлена.")

    async def run(self, link, reason_type, comment, report):
        await self.run_sequential(
            lambda session, report: self.report_one(session, link, reason_type, comment, report),
            report,
            pause="delay",
        )
