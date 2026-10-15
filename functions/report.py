from modules.console import console
from rich.prompt import Prompt

from telethon import types, functions, utils
from functions.base import TelethonFunction
from functions.base.base import console_report


class ReportFunc(TelethonFunction):
    """Moderation report (message/post)"""

    @staticmethod
    def post_id(link: str) -> int | None:
        """The post a link points at (t.me/<name>/<id>, t.me/c/<chat>/<id>, a forum topic's post,
        ?comment=…), so its number needn't be typed again; None for a chat's own link or @name."""
        parts = link.split("?")[0].rstrip("/").split("/")
        if len(parts) >= 2 and parts[-2] == "c":  # t.me/c/<chat>: the private chat itself, as report_peer
            return None
        try:
            return TelethonFunction.parse_message_link(link)[1]
        except (ValueError, IndexError):  # @name, t.me/name: no post in it
            return None

    def report_peer(self, peer):
        """The chat of a post link (t.me/<name>/<id>, t.me/c/<id>/<id>): Telethon resolves a chat's
        link or @name, but not a post link nor a -100… id (it reads one as a phone number). Anything
        else is passed on as is."""
        if isinstance(peer, str):
            if peer.startswith("-") and peer[1:].isdigit():  # a chat's id, as the scraper takes it
                short, peer_type = utils.resolve_id(int(peer))
                return peer_type(short)
            parts = peer.split("?")[0].rstrip("/").split("/")
            if len(parts) >= 2 and parts[-2] == "c" and parts[-1].isdigit():  # a private chat's own link
                return types.PeerChannel(int(parts[-1]))
            try:
                return self.parse_message_link(peer)[0]
            except (ValueError, IndexError):  # a chat link, @name
                pass
        return peer

    async def report_step(self, session, peer, ids, comment, option):
        return await session(functions.messages.ReportRequest(
            peer=await self.resolve_chat(session, self.report_peer(peer)),
            id=ids,
            option=option,
            message=comment
        ))

    async def walk(self, session, peer, ids, comment, option, choose):
        """Follow the report flow from `option`: a comment step goes on by itself, a menu by
        choose(result) -> the option's index, or None to stop there. Returns ("choose", options)
        when stopped at a menu, ("done", None) once the report is submitted."""
        while True:
            result = await self.report_step(session, peer, ids, comment, option)

            if isinstance(result, types.ReportResultAddComment):
                option = result.option
                continue

            if isinstance(result, types.ReportResultChooseOption):
                index = choose(result)
                if index is None:
                    return "choose", result.options
                option = result.options[index].option
                continue

            return "done", None  # ReportResultReported (or an unknown result: nothing to follow)

    async def resolve_and_report(self, session, peer, ids, comment):
        """Walk the report flow interactively on the first account; record menu choices."""
        selections = []

        def ask(result):
            console.print(f"\n[bold white]{result.title}[/]")
            for i, opt in enumerate(result.options):
                console.print(f"[bold white][{i + 1}] {opt.text}[/]")

            choice = console.input("[bold white]>> ")

            while not (choice.isdigit() and 1 <= int(choice) <= len(result.options)):
                choice = console.input("[bold white]>> ")

            selections.append(int(choice) - 1)
            return selections[-1]

        await self.walk(session, peer, ids, comment, b"", ask)
        return selections

    async def replay(self, session, peer, ids, comment, selections):
        """Repeat the recorded report path on another account (menu choices by index). ValueError if
        Telegram asks this one a menu the path has no choice for: its report is not sent rather than sent
        under a reason nobody picked."""
        recorded = iter(selections)
        status, _ = await self.walk(session, peer, ids, comment, b"", lambda result: next(recorded, None))
        if status == "choose":
            raise ValueError("Telegram показал этому аккаунту другой путь жалобы — жалоба не отправлена")

    async def step(self, state, option):
        """Advance the report flow one step on the first worker (bot-driven).

        Auto-follows AddComment. Returns ("choose", options) when the operator must
        pick, or ("done", None) when the report is submitted.
        """
        return await self.walk(state["session"], state["peer"], state["ids"], state["comment"], option,
                               lambda result: None)

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
                me = await self.poll_me(session, report)
                if me is None:
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

        link = Prompt.ask("[bold red]ссылка на канал или чат — или сразу на пост[/]")
        post = self.post_id(link)
        while True:
            parts = [p.strip() for p in (Prompt.ask(
                "[bold red]номера постов через запятую — число в конце ссылки на пост (t.me/name/123 → 123)[/]",
                default=str(post) if post is not None else None) or "").split(",") if p.strip()]
            if parts and all(p.isdigit() for p in parts):
                posts = [int(p) for p in parts]
                break

        comment = console.input("[bold red]комментарий для модераторов Telegram (необязательно)> [/]")

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
