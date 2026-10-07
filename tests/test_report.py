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


def test_tally_negated_success_word_is_an_error():
    # "not changed" contains "changed": the error check must win
    assert _tally("[A] not changed: USERNAME_OCCUPIED", "[A] not cleared: x", "[A] not hidden: x",
                  "[A] not voted: x", "[!] [acc 1] no linked chat",
                  "[A] no invite rights in destination") == (0, 6)


def test_tally_counts_profile_and_activity_success_lines():
    assert _tally("[A] last seen hidden", "[A] personal channel cleared", "[A] voted",
                  "[A] username set: @x", "Reset authorization 1.2.3.4 (PC, Windows)",
                  "[acc 1] joined") == (6, 0)


def test_tally_ignores_neutral_lines():
    # job totals and per-account notes are not counted again
    assert _tally("Done: 5/5 accounts", "[acc 1] captcha solved", "[acc 1] no captcha in 30s",
                  "[-] [@a] Account restricted until: 1 Nov 2026") == (0, 0)


def test_tally_spambot_check_lines():
    # a quoted @SpamBot reply is not a result, whatever words it has; a dead session is an error
    assert _tally("✅ @a — без ограничений", "⛔ @b — ограничен бессрочно") == (1, 0)
    assert _tally("💬 Ответ @SpamBot (@b):\nyou can't send messages, error") == (0, 0)
    assert _tally("💀 x.jsession — сессия мертва") == (0, 1)


def test_tally_counts_function_success_lines():
    assert _tally("added. user_id=1 total: 1", "[+] Account active (no restriction)",
                  "[SUCCESS] [A] : Reaction was sent", "[A] Photo uploaded successfully (p)") == (4, 0)


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
