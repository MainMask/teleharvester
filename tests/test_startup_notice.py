"""The admins hear of every bot start, and of a start after a crash (bot/app.py)."""

import asyncio
import types

from bot import app


class _Bot:
    def __init__(self, fail_for=()):
        self.sent, self.fail_for = [], set(fail_for)

    async def send_message(self, chat_id, text, **kwargs):
        if chat_id in self.fail_for:  # an admin who never opened the bot
            raise RuntimeError("Forbidden: bot can't initiate conversation with a user")
        self.sent.append((chat_id, text))


POOL = types.SimpleNamespace(all=lambda: [1, 2], workers=lambda: 3)  # every project's workers (Workspaces)


def test_clean_start_is_announced_and_marked(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bot = _Bot()
    asyncio.run(app.announce_start(bot, {1}, POOL))

    assert bot.sent == [(1, "🔄 Бот запущен · проектов: 2 · воркеров: 3")]
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


def test_the_marker_is_dropped_after_every_other_shutdown_step():
    # a stop that hangs until systemd kills the bot must leave the marker: the next start says so
    import inspect

    shutdown = [line.split("#")[0].strip() for line in inspect.getsource(app.run_bot).splitlines()
                if line.strip().startswith("dp.shutdown.register(")]
    assert shutdown[-1] == "dp.shutdown.register(mark_stopped)" and len(shutdown) == 6
    # a watch stopped by the shutdown is kept for the next start; the workers' jobs before the scrapes' long wait
    assert shutdown.index("dp.shutdown.register(chat_broadcasts.stop_watches)") \
        < shutdown.index("dp.shutdown.register(stop_jobs)") \
        < shutdown.index("dp.shutdown.register(scraping.stop_for_shutdown)")


class _Slot:
    """A workspace's `manager`: stopped, its job ends after `ticks` turns of the loop (None: never)."""

    def __init__(self, active=True, ticks=1):
        self.active, self.ticks, self.stopped = active, ticks, False

    async def stop(self):
        if not self.active:
            return False
        self.stopped = True

        async def finish():  # its last message, then the slot is free
            for _ in range(self.ticks):
                await asyncio.sleep(0.01)
            self.active = False
        if self.ticks is not None:
            self._task = asyncio.create_task(finish())
        return True


def _workspaces(*slots):
    return types.SimpleNamespace(all=lambda: [types.SimpleNamespace(manager=slot) for slot in slots])


def test_shutdown_stops_the_workers_jobs_and_waits_for_their_last_word():
    """Left to asyncio.run's cancellation, after aiogram closed its session, a job's last message would open a new
    one nobody closes."""
    running, idle = _Slot(ticks=3), _Slot(active=False)
    asyncio.run(app.stop_jobs(_workspaces(running, idle)))
    assert running.stopped and not running.active and not idle.stopped


def test_shutdown_does_not_wait_past_its_grace_for_a_job_that_hangs(monkeypatch):
    monkeypatch.setattr(app, "JOBS_STOP_GRACE", 0.05)  # the scrapes' wait follows, within systemd's 30 s
    hanging = _Slot(ticks=None)

    async def timed():
        loop = asyncio.get_running_loop()
        start = loop.time()
        await app.stop_jobs(_workspaces(hanging))
        return loop.time() - start
    assert asyncio.run(timed()) < 1 and hanging.active
