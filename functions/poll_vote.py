import asyncio

from modules.console import console

from telethon import functions
from functions.base import TelethonFunction
from functions.base.base import console_report
from scraper.scrape import CHAT_LOOKUP_TIMEOUT


class PollVoteFunc(TelethonFunction):
    """Vote in poll"""

    async def vote(self, session, channel, post_id, option_number, report, comment=None):
        async with self.storage.ainitialize_session(session):
            me = await self.poll_me(session, report)
            if me is None:
                return False

            try:
                channel, post_id = await self.resolve_message(session, channel, post_id, comment)
                message = await session.get_messages(channel, ids=post_id)
                option = message.poll.poll.answers[option_number].option

                # safe_call waits out short FloodWaits so the vote lands; a long wait
                # raises AccountLimited, caught by the except below and reported.
                await self.safe_call(lambda: session(
                    functions.messages.SendVoteRequest(
                        peer=channel,
                        msg_id=post_id,
                        options=[option]
                    )
                ))
            except Exception as err:
                self.progress_failed()
                await report(f"[{me.first_name}] не проголосовал: {err}")
                return False

            self.progress_ok()
            await report(f"[{me.first_name}] проголосовал")
            return True

    async def answers(self, session, link) -> list[str]:
        """The poll's options as its voters see them (the bot offers them as buttons), read on `session`."""
        channel, post_id = await self.resolve_message(session, *self.parse_message_link(link), self.comment_id(link))
        message = await session.get_messages(channel, ids=post_id)
        if message is None or message.poll is None:
            raise ValueError("в этом сообщении нет опроса")
        return [answer.text.text for answer in message.poll.poll.answers]

    async def _read_answers(self, link) -> list[str] | str:
        """The poll's options, read on the first worker; why not, when they can't be read."""
        session = self.sessions[0]
        try:
            async with self.storage.ainitialize_session(session):
                return await asyncio.wait_for(self.answers(session, link), CHAT_LOOKUP_TIMEOUT)
        except (Exception, SystemExit) as err:  # SystemExit: a logged-out session
            return str(err) or "Telegram не ответил вовремя"

    async def run(self, link, option_number, report):
        channel, post_id = self.parse_message_link(link)
        comment = self.comment_id(link)  # a poll in a comment: its link carries ?comment=

        results = await self.run_sequential(
            lambda session, report: self.vote(session, channel, post_id, option_number, report, comment),
            report,
            pause="delay",
        )

        # no ok/error keyword: the per-account lines above are what the job summary counts
        await report(f"Итого: {sum(results)}/{len(self.sessions)} аккаунтов")

    async def execute(self):
        post_link = self.ask_message_link("[bold red]ссылка на сообщение/пост> ")
        # as the bot's: the options shown, read on the first worker; unread, the number is typed as before
        answers = await self._read_answers(post_link)
        if isinstance(answers, str):
            console.print(f"ℹ️ Варианты не прочитаны: {answers.rstrip('.')}.", markup=False, highlight=False)
        else:
            console.print("\n".join(f"[{number}] {text}" for number, text in enumerate(answers, 1)),
                          markup=False, highlight=False)
        while True:
            option_number = console.input("[bold red]номер варианта ответа (например, 1 или 2)> ")
            if not (option_number.isdigit() and int(option_number) >= 1):
                continue
            if isinstance(answers, list) and int(option_number) > len(answers):
                console.print(f"Такого варианта нет: в опросе их {len(answers)}.", style="bold red", markup=False)
                continue
            break

        option_number = int(option_number) - 1

        with console.status("Голосование"):
            await self.run(post_link, option_number, console_report)

        
