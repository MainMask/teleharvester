from modules.console import console
from rich.prompt import Prompt

from telethon import types, functions
from functions.base import TelethonFunction
from functions.base.base import console_report


class ReportFunc(TelethonFunction):
    """Moderation report (message/post)"""

    def report_peer(self, peer):
        """The chat of a post link (t.me/<name>/<id>, t.me/c/<id>/<id>): Telethon resolves a chat's
        link, @name or id, but not a post link. Anything else is passed on as is."""
        if isinstance(peer, str):
            try:
                return self.parse_message_link(peer)[0]
            except (ValueError, IndexError):  # a chat link, @name, -100… id
                pass
        return peer

    async def report_step(self, session, peer, ids, comment, option):
        return await session(functions.messages.ReportRequest(
            peer=self.report_peer(peer),
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

                choice = console.input("[bold white]>> ")

                while not (choice.isdigit() and 1 <= int(choice) <= len(result.options)):
                    choice = console.input("[bold white]>> ")

                choice = int(choice) - 1
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

    async def step(self, state, option):
        """Advance the report flow one step on the first worker (bot-driven).

        Auto-follows AddComment. Returns ("choose", options) when the operator must
        pick, or ("done", None) when the report is submitted.
        """
        while True:
            result = await self.report_step(
                state["session"], state["peer"], state["ids"], state["comment"], option
            )

            if isinstance(result, types.ReportResultReported):
                return "done", None

            if isinstance(result, types.ReportResultAddComment):
                option = result.option
                continue

            if isinstance(result, types.ReportResultChooseOption):
                return "choose", result.options

            return "done", None

    async def replay_rest(self, sessions, peer, ids, comment, selections, report, stop=None):
        """Replay the recorded report path on the remaining workers; `stop` (the bot's ⏹, an
        asyncio.Event) skips the ones not reached yet. True if any were skipped so."""
        for session in sessions:
            # the first worker already reported before this call; space the rest out so the
            # reports don't all land within the same second (a tell-tale cluster signal)
            await self.delay()
            if stop is not None and stop.is_set():  # checked after the wait: a ⏹ during it counts
                return True
            async with self.storage.ainitialize_session(session):
                try:
                    me = await self.get_me(session)
                except Exception as err:
                    self.progress_failed()
                    await report(f"не удалось опросить аккаунт: {err}")
                    self.progress_step()
                    continue
                try:
                    await self.replay(session, peer, ids, comment, selections)
                except Exception as err:
                    self.progress_failed()
                    await report(f"[{me.first_name}] ошибка: {err}")
                else:
                    self.progress_ok()
                    await report(f"[{me.first_name}] жалоба отправлена.")
                self.progress_step()
        return False

    async def execute(self):
        self.ask_accounts_count()

        if not self.sessions:
            return

        link = Prompt.ask("[bold red]ссылка на пост или канал[/]")
        while True:
            parts = [p.strip() for p in Prompt.ask("[bold red]id постов через запятую[/]").split(",") if p.strip()]
            if parts and all(p.isdigit() for p in parts):
                posts = [int(p) for p in parts]
                break

        comment = console.input("[bold red]комментарий> [/]")

        first, rest = self.sessions[0], self.sessions[1:]

        async with self.storage.ainitialize_session(first):
            try:
                me = await self.get_me(first)
            except Exception as err:  # a dead proxy / session: say so instead of a bare traceback
                console.print(f"[bold red]не удалось опросить аккаунт:[/] {self.safe(err)}")
                return

            try:
                selections = await self.resolve_and_report(first, link, posts, comment)
            except Exception as err:
                console.print(
                    "[{name}] [bold red]ошибка:[/] {error}"
                    .format(name=self.safe(me.first_name), error=self.safe(err))
                )
                return

            console.print(f"[{self.safe(me.first_name)}] [bold green]жалоба отправлена.[/]")

        await self.replay_rest(rest, link, posts, comment, selections, console_report)
