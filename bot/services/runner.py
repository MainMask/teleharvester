import asyncio
import time


class TelegramReporter:
    """A progress reporter that edits one Telegram message as a job reports lines.

    Used as the `report` callback passed to a function's run(): `await report(text)`.
    Edits are throttled; only the last `max_lines` lines are kept so the message
    stays within Telegram's size limit.
    """

    def __init__(self, bot, chat_id, header="Выполняется…", max_lines=25, min_interval=1.2, reply_markup=None):
        self.bot = bot
        self.chat_id = chat_id
        self.header = header
        self.max_lines = max_lines
        self.min_interval = min_interval
        self.reply_markup = reply_markup

        self.lines: list[str] = []
        self.message_id = None
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def start(self):
        message = await self.bot.send_message(self.chat_id, self.header, reply_markup=self.reply_markup)
        self.message_id = message.message_id

    async def __call__(self, text: str):
        async with self._lock:
            self.lines.append(text)
            self.lines = self.lines[-self.max_lines:]

            if time.monotonic() - self._last >= self.min_interval:
                await self._flush()

    async def _flush(self):
        self._last = time.monotonic()
        body = "\n".join(self.lines) or "…"

        try:
            await self.bot.edit_message_text(
                f"{self.header}\n\n{body}",
                chat_id=self.chat_id,
                message_id=self.message_id,
                reply_markup=self.reply_markup,
            )
        except Exception:
            pass  # ignore "message is not modified" and transient edit errors

    async def finish(self, summary: str):
        async with self._lock:
            self.header = summary
            self.reply_markup = None  # drop the Stop button once the job is over
            await self._flush()
