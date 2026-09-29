from rich.progress import track
from rich.console import Console
from rich.prompt import Prompt

from telethon import types, functions
from functions.base import TelethonFunction

console = Console()


class ReportFunc(TelethonFunction):
    """Moderation report (message/post)"""

    async def report_step(self, session, peer, ids, comment, option):
        return await session(functions.messages.ReportRequest(
            peer=peer,
            id=ids,
            option=option,
            message=comment
        ))

    async def resolve_and_report(self, session, peer, ids, comment):
        """Walk the report flow interactively on the first account; record menu choices."""
        option = b""
        selections = []

        while True:
            result = await self.report_step(session, peer, ids, comment, option)

            if isinstance(result, types.ReportResultReported):
                return selections

            if isinstance(result, types.ReportResultAddComment):
                option = result.option
                continue

            if isinstance(result, types.ReportResultChooseOption):
                console.print(f"\n[bold white]{result.title}[/]")
                for i, opt in enumerate(result.options):
                    console.print(f"[bold white][{i + 1}] {opt.text}[/]")

                choice = int(console.input("[bold white]>> ")) - 1
                selections.append(choice)
                option = result.options[choice].option
                continue

            return selections

    async def replay(self, session, peer, ids, comment, selections):
        """Repeat the recorded report path on another account (menu choices by index)."""
        option = b""
        step = 0

        while True:
            result = await self.report_step(session, peer, ids, comment, option)

            if isinstance(result, types.ReportResultReported):
                return

            if isinstance(result, types.ReportResultAddComment):
                option = result.option
                continue

            if isinstance(result, types.ReportResultChooseOption):
                idx = selections[step] if step < len(selections) else 0
                option = result.options[idx].option
                step += 1
                continue

            return

    async def execute(self):
        self.ask_accounts_count()

        if not self.sessions:
            return

        link = Prompt.ask("[bold red]link[/]")
        posts = Prompt.ask("[bold red]enter the post ids[/]")
        posts = [int(i) for i in posts.split(",")]

        comment = console.input("[bold red]comment> [/]")

        first, rest = self.sessions[0], self.sessions[1:]

        async with self.storage.ainitialize_session(first):
            me = await first.get_me()
            try:
                selections = await self.resolve_and_report(first, link, posts, comment)
            except Exception as err:
                console.print(
                    "[{name}] [bold red]error.[/] {error}"
                    .format(name=self.safe(me.first_name), error=self.safe(err))
                )
                return

            console.print(f"[{self.safe(me.first_name)}] [bold green]submitted.[/]")

        for session in track(rest, "[yellow]Submitting...[/]", total=len(rest)):
            async with self.storage.ainitialize_session(session):
                me = await session.get_me()
                try:
                    await self.replay(session, link, posts, comment, selections)
                except Exception as err:
                    console.print(
                        "[{name}] [bold red]error.[/] {error}"
                        .format(name=self.safe(me.first_name), error=self.safe(err))
                    )
                else:
                    console.print(f"[{self.safe(me.first_name)}] [bold green]submitted.[/]")
