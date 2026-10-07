from modules.console import console
from rich.prompt import Prompt

from telethon import types, functions
from functions.base import TelethonFunction
from functions.base.base import console_report


class ReportUserFunc(TelethonFunction):
    """Moderation report (user)"""

    def __init__(self, storage, settings):
        super().__init__(storage, settings)

        self.reasons = (
            ("Насилие над детьми", types.InputReportReasonChildAbuse()),
            ("Авторские права", types.InputReportReasonCopyright()),
            ("Фейковый канал/аккаунт", types.InputReportReasonFake()),
            ("Порнография", types.InputReportReasonPornography()),
            ("Спам", types.InputReportReasonSpam()),
            ("Насилие", types.InputReportReasonViolence()),
            ("Другое", types.InputReportReasonOther())
        )

    async def report_one(self, session, link, reason_type, comment, report):
        async with self.storage.ainitialize_session(session):
            try:
                me = await self.get_me(session)
            except Exception as err:
                self.progress_failed()
                await report(f"не удалось опросить аккаунт: {err}")
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
            pause=self.settings.delay,
        )

    async def execute(self):
        self.ask_accounts_count()

        link = Prompt.ask("[bold red]username или ссылка>[/]")

        print()

        for index, reasons in enumerate(self.reasons):
            reason, _ = reasons

            console.print(
                "[bold white][{}] {}[/]"
                .format(index + 1, reason)
            )

        print()

        choice = console.input("[bold white]>> [/]")

        while not (choice.isdigit() and 1 <= int(choice) <= len(self.reasons)):
            choice = console.input("[bold white]>> [/]")

        reason_type = self.reasons[int(choice) - 1][1]

        comment = console.input("[bold red]комментарий> [/]")

        await self.run(link, reason_type, comment, console_report)
