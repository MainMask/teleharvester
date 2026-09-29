from rich.progress import track
from rich.console import Console
from rich.prompt import Prompt

from telethon import types, functions
from functions.base import TelethonFunction

console = Console()

class ReportUserFunc(TelethonFunction):
    """Moderation report (user)"""

    def __init__(self, storage, settings):
        super().__init__(storage, settings)

        self.reasons = (
            ("Child abuse", types.InputReportReasonChildAbuse()),
            ("Copyright", types.InputReportReasonCopyright()),
            ("Fake channel/account", types.InputReportReasonFake()),
            ("Pornography", types.InputReportReasonPornography()),
            ("Spam", types.InputReportReasonSpam()),
            ("Violence", types.InputReportReasonViolence()),
            ("Other", types.InputReportReasonOther())
        )

    async def execute(self):
        self.ask_accounts_count()

        link = Prompt.ask("[bold red]username>[/]")

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

        comment = console.input("[bold red]comment> [/]")

        for index, session in track(
            enumerate(self.sessions),
            "[yellow]Submitting...[/]",
            total=len(self.sessions)
        ):
            async with self.storage.ainitialize_session(session):
                me = await session.get_me()
                try:
                    await session(
                        functions.account.ReportPeerRequest(
                            peer=link,
                            reason=reason_type,
                            message=comment
                        )
                    )
                except Exception as err:
                    console.print(
                        "[{name}] [bold red]error.[/] {error}"
                        .format(name=self.safe(me.first_name), error=self.safe(err))
                    )
