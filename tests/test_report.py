"""End-of-job report: TelegramReporter.finish sends a separate summary message + menu."""

import asyncio
import types

from bot.services.progress import Progress
from bot.services.runner import TelegramReporter


def ns(**kw):
    return types.SimpleNamespace(**kw)


class _Bot:
    def __init__(self):
        self.sent = []       # (text, reply_markup)
        self.edits = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((text, reply_markup))
        return ns(message_id=len(self.sent))

    async def edit_message_text(self, text, **kwargs):
        self.edits.append(text)
        return None


def test_finish_sends_report_with_counts_and_menu():
    bot = _Bot()
    markup = object()
    progress = Progress()
    reporter = TelegramReporter(
        bot, 1, header="Рассылка…", min_interval=0,
        job_label="Рассылка", workers=5, final_markup=markup, progress=progress,
    )

    async def scenario():
        await reporter.start()
        progress.ok, progress.failed = 2, 1  # as the function counted them
        await reporter("[A] отправлено: user1")
        await reporter.finish("Готово ✅")

    asyncio.run(scenario())

    report_text, report_markup = bot.sent[-1]
    assert report_markup is markup
    assert "Готово ✅" in report_text
    assert "Задача: Рассылка" in report_text
    assert "Воркеров: 5" in report_text
    assert "Успешно: 2" in report_text
    assert "Ошибок: 1" in report_text
    assert "Время:" in report_text


def test_no_report_before_start():
    # finish() without start() (job never ran) must not send a report message
    bot = _Bot()
    reporter = TelegramReporter(bot, 1, header="X", min_interval=0)

    asyncio.run(reporter.finish("⏹ Остановлено"))

    assert bot.sent == []


def test_report_omits_zero_counters():
    bot = _Bot()
    reporter = TelegramReporter(bot, 1, header="Задача", min_interval=0, workers=2)

    async def scenario():
        await reporter.start()
        await reporter.finish("Готово ✅")

    asyncio.run(scenario())

    report_text = bot.sent[-1][0]
    assert "Успешно" not in report_text
    assert "Ошибок" not in report_text
    assert "Воркеров: 2" in report_text


def test_job_manager_run_emits_report():
    from bot.services.jobs import JobManager

    class _Pool:
        @property
        def workers(self):
            return ["w1", "w2"]

        async def run(self, instance, bot_function, factory, report, only=None):
            instance.sessions = self.workers  # as WorkerPool.delegate
            await factory(instance)

    async def scenario():
        m = JobManager()
        bot = _Bot()

        async def job(f, r):
            await r("[A] sent. done")

        await m.run(bot, 1, _Pool(), ns(), ns(risk="safe"), job, "Инвайт", "Готово ✅")
        await asyncio.sleep(0.05)
        return bot

    bot = asyncio.run(scenario())
    assert any("Задача: Инвайт" in text for text, _ in bot.sent)
    assert any("Воркеров: 2" in text for text, _ in bot.sent)


def test_line_wording_does_not_count():
    # the counts are the function's own (progress_ok / progress_failed), not guessed from the
    # text: a total like "Готово. Добавлено…" or "3 ошибки подряд" adds nothing
    bot = _Bot()
    reporter = TelegramReporter(bot, 1, header="Задача", min_interval=0, progress=Progress())

    async def scenario():
        await reporter.start()
        for line in ("Готово. Добавлено контактов: 4", "[A] 3 ошибки подряд, остановка.",
                     "[A] отправлено, всего: 3", "[!] FloodWait"):
            await reporter(line)
        await reporter.finish("Готово ✅")

    asyncio.run(scenario())

    report_text = bot.sent[-1][0]
    assert "Успешно" not in report_text and "Ошибок" not in report_text


def test_job_manager_reports_the_functions_counts():
    from bot.services.jobs import JobManager

    class _Pool:
        workers = ["w1"]

        async def run(self, instance, bot_function, factory, report, only=None):
            instance.sessions = self.workers
            await factory(instance)

    async def scenario():
        bot = _Bot()

        async def job(func, reporter):
            func.progress.ok += 3  # what BaseFunction.progress_ok does
            func.progress.failed += 1

        await JobManager().run(bot, 1, _Pool(), ns(), ns(risk="safe"), job, "Инвайт", "Готово ✅")
        await asyncio.sleep(0.05)
        return bot

    report_text = asyncio.run(scenario()).sent[-1][0]
    assert "Успешно: 3" in report_text and "Ошибок: 1" in report_text


def test_report_counts_the_workers_the_job_got():
    """A worker the scraper holds is left out of the job, and out of the summary's count."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    from bot.services.delegation import WorkerPool
    from bot.services.jobs import JobManager

    async def scenario():
        bot = _Bot()
        a, b = (TelegramClient(StringSession(), 1, "x") for _ in range(2))
        paths = {id(a): "sessions/a.jsession", id(b): "sessions/b.jsession"}
        pool = WorkerPool(ns(sessions=[a, b], get_session_path=lambda c: paths[id(c)]))
        pool.scraping = ns(path="sessions/a.jsession", label="A")

        async def job(f, r):
            await r("[B] sent. done")

        await JobManager().run(bot, 1, pool, ns(), ns(risk="safe"), job, "Инвайт", "Готово ✅")
        await asyncio.sleep(0.05)
        return bot

    bot = asyncio.run(scenario())
    assert any("Воркеров: 1" in text for text, _ in bot.sent)
