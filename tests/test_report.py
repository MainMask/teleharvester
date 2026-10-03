"""End-of-job report: TelegramReporter.finish sends a separate summary message + menu."""

import asyncio
import types

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
    reporter = TelegramReporter(
        bot, 1, header="Рассылка…", min_interval=0,
        job_label="Рассылка", workers=5, final_markup=markup,
    )

    async def scenario():
        await reporter.start()
        await reporter("[A] sent. user1")       # ok
        await reporter("[A] sent. user2")       # ok
        await reporter("[B] not sent. user3")   # error
        await reporter("нейтральная строка")    # neutral
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

        async def run(self, instance, bot_function, factory, report):
            await factory(instance)

    async def scenario():
        m = JobManager()
        bot = _Bot()

        async def job(f, r):
            await r("[A] sent. done")

        await m.run(bot, 1, _Pool(), object(), ns(risk="safe"), job, "Инвайт", "Готово ✅")
        await asyncio.sleep(0.05)
        return bot

    bot = asyncio.run(scenario())
    assert any("Задача: Инвайт" in text for text, _ in bot.sent)
    assert any("Воркеров: 2" in text for text, _ in bot.sent)


def test_report_submissions_count_as_success():
    bot = _Bot()
    reporter = TelegramReporter(bot, 1, header="Репорт…", min_interval=0, job_label="Репорт")

    async def scenario():
        await reporter.start()
        await reporter("[A] submitted.")
        await reporter.finish("Репорт отправлен ✅")

    asyncio.run(scenario())

    assert "Успешно: 1" in bot.sent[-1][0]


def _tally(*lines):
    reporter = TelegramReporter(_Bot(), 1, min_interval=0)
    for line in lines:
        reporter._tally(line)
    return reporter._ok, reporter._errors


def test_tally_matches_whole_words_only():
    # "unlimited" is not "limit", "present" is not "sent"
    assert _tally("[A] unlimited plan", "[B] present in chat") == (0, 0)


def test_tally_counts_function_error_lines():
    assert _tally("[!] FloodWait", "[A] can't read participants: x",
                  "[A] couldn't find a free username", "[A] skip 42: x") == (0, 4)


def test_tally_counts_function_success_lines():
    assert _tally("added. user_id=1 total: 1", "[+] Account active (no restriction)",
                  "[SUCCESS] [A] : Reaction was sent", "[A] Photo uploaded successfully (p)") == (4, 0)
