"""The admins hear of every bot start, and of a start after a crash (bot/app.py)."""

import asyncio
import types

from bot import app


class _Bot:
    def __init__(self, fail_for=()):
        self.sent, self.fail_for = [], set(fail_for)

    async def send_message(self, chat_id, text):
        if chat_id in self.fail_for:  # an admin who never opened the bot
            raise RuntimeError("Forbidden: bot can't initiate conversation with a user")
        self.sent.append((chat_id, text))


POOL = types.SimpleNamespace(count=lambda: 3)


def test_clean_start_is_announced_and_marked(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bot = _Bot()
    asyncio.run(app.announce_start(bot, {1}, POOL))

    assert bot.sent == [(1, "🔄 Бот запущен · воркеров: 3")]
    assert (tmp_path / app.RUNNING_MARKER).exists()


def test_start_after_a_crash_says_so(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    asyncio.run(app.announce_start(_Bot(), {1}, POOL))  # ...then killed: no mark_stopped

    bot = _Bot()
    asyncio.run(app.announce_start(bot, {1}, POOL))
    assert "после сбоя" in bot.sent[0][1] and "воркеров: 3" in bot.sent[0][1]


def test_clean_stop_clears_the_marker(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    asyncio.run(app.announce_start(_Bot(), {1}, POOL))
    asyncio.run(app.mark_stopped())
    asyncio.run(app.mark_stopped())  # twice: no error

    bot = _Bot()
    asyncio.run(app.announce_start(bot, {1}, POOL))
    assert bot.sent[0][1].startswith("🔄 Бот запущен")


def test_one_unreachable_admin_does_not_stop_the_others(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bot = _Bot(fail_for={1})
    asyncio.run(app.announce_start(bot, {1, 2}, POOL))
    assert [chat for chat, _ in bot.sent] == [2]
