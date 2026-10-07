import asyncio
import re
import time

from aiogram.exceptions import TelegramRetryAfter

# Best-effort classification of a progress line for the end-of-job summary.
# Whole words only, so "unlimited" / "present" don't count as "limit" / "sent".
_ERROR_RE = re.compile(
    r"⚠️|❌|💀|\[!\]|\bfailed\b|\berrors?\b|\bnot (?:sent|changed|cleared|hidden|saved|voted)\b"
    r"|\blimit\b|\bcan't\b|\bcouldn't\b|\bskip\b|\bbanned\b|\bno invite rights\b|\bnot a supergroup\b"
    r"|не удал|не отправ|ошибк",
    re.IGNORECASE,
)
_OK_RE = re.compile(
    r"✅|\[\+\]|\bsent\b|\bsubmitted\b|\binvited\b|\bjoined\b|\breacted\b|\bchanged\b"
    r"|\bupdated\b|\bsuccess(?:fully)?\b|\badded\b|\bdeleted\b|\buploaded\b"
    r"|\bhidden\b|\bcleared\b|\bvoted\b|\bset\b|\breset\b"
    r"|отправлен|приглаш|готово",
    re.IGNORECASE,
)


class TelegramReporter:
    """A progress reporter that edits one Telegram message as a job reports lines.

    Used as the `report` callback passed to a function's run(): `await report(text)`.
    Edits are throttled; only the last `max_lines` lines are kept so the message
    stays within Telegram's size limit.
    """

    def __init__(self, bot, chat_id, header="Выполняется…", max_lines=25, min_interval=1.2,
                 reply_markup=None, job_label=None, workers=None, final_markup=None):
        self.bot = bot
        self.chat_id = chat_id
        self.header = header
        self.max_lines = max_lines
        self.min_interval = min_interval
        self.reply_markup = reply_markup

        self.job_label = job_label or header
        self.workers = workers
        self.final_markup = final_markup

        self.lines: list[str] = []
        self.message_id = None
        self._last = 0.0
        self._lock = asyncio.Lock()
        self._pending = None  # delayed flush for lines that arrived inside min_interval

        self._started = None  # monotonic start time; set in start()
        self._ok = 0
        self._errors = 0

    async def start(self):
        self._started = time.monotonic()
        message = await self.bot.send_message(self.chat_id, self.header, reply_markup=self.reply_markup)
        self.message_id = message.message_id

    def _tally(self, text: str):
        if text.startswith("💬"):  # a quote (a @SpamBot reply), not a result
            return
        if _ERROR_RE.search(text):
            self._errors += 1
        elif _OK_RE.search(text):
            self._ok += 1

    async def __call__(self, text: str):
        async with self._lock:
            self._tally(text)
            self.lines.append(text)
            self.lines = self.lines[-self.max_lines:]

            wait = self.min_interval - (time.monotonic() - self._last)
            if wait <= 0:
                await self._flush()
            elif self._pending is None:
                # else a burst's tail stays hidden until the next report (hours, for a listener)
                self._pending = asyncio.create_task(self._flush_later(wait))

    async def _flush_later(self, wait):
        await asyncio.sleep(wait)
        async with self._lock:
            self._pending = None
            await self._flush()

    async def _flush(self, final=False, attempts=3):
        self._last = time.monotonic()
        header = self.header[:1000]  # a huge error text must leave room for the body
        body = "\n".join(self.lines) or "…"
        # keep the newest lines within 4096 UTF-16 units (an emoji is 2); errors="ignore"
        # drops a surrogate pair cut in half
        budget = 4096 - len(header.encode("utf-16-le")) // 2 - 2
        body = body.encode("utf-16-le")[-budget * 2:].decode("utf-16-le", errors="ignore")

        try:
            await self.bot.edit_message_text(
                f"{header}\n\n{body}",
                chat_id=self.chat_id,
                message_id=self.message_id,
                reply_markup=self.reply_markup,
            )
        except TelegramRetryAfter as err:
            # final: the result and the Stop-button removal must not be lost; a long wait
            # isn't waited out, as it would hold the bot's single job slot meanwhile
            if final:
                if attempts > 1 and err.retry_after <= 60:
                    await asyncio.sleep(err.retry_after)
                    await self._flush(final=True, attempts=attempts - 1)
            else:  # no progress edits until the wait is over: each one would prolong it
                self._last = time.monotonic() + err.retry_after
                if self._pending is None:  # ...but show the held-back lines once it is
                    self._pending = asyncio.create_task(self._flush_later(err.retry_after))
        except Exception:
            pass  # ignore "message is not modified" and transient edit errors

    def _build_report(self, summary: str) -> str:
        lines = [summary, f"📋 Задача: {self.job_label}"]
        if self.workers is not None:
            lines.append(f"🤖 Воркеров: {self.workers}")
        if self._ok:
            lines.append(f"✅ Успешно: {self._ok}")
        if self._errors:
            lines.append(f"⚠️ Ошибок: {self._errors}")
        elapsed = int(time.monotonic() - self._started) if self._started else 0
        lines.append(f"⏱ Время: {elapsed // 60:02d}:{elapsed % 60:02d}")
        return "\n".join(lines)

    async def finish(self, summary: str):
        async with self._lock:
            self.header = summary
            self.reply_markup = None  # drop the Stop button once the job is over
            if self._pending is not None:
                self._pending.cancel()
                self._pending = None
            await self._flush(final=True)

        if self._started is not None:  # a job actually ran: send a separate report + menu
            try:
                await self.bot.send_message(
                    self.chat_id,
                    self._build_report(summary),
                    reply_markup=self.final_markup,
                )
            except Exception:
                pass
