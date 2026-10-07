from telethon import TelegramClient

from .registry import RISKY, BotFunction


class HostActionBlocked(Exception):
    """A risky action was about to run on something that is not a delegated worker."""


class WorkerPool:
    """The single entry point for running a function on worker accounts.

    Host protection: the bot (Bot API) performs no MTProto actions and never appears
    here. This class enforces that invariant explicitly — every executor must be a
    Telethon worker from sessions/, and RISKY functions refuse to run with no workers.
    """

    def __init__(self, sessions_storage):
        self.storage = sessions_storage
        # the ScrapeAccount of the worker the scraper runs on (bot/routers/scraping.py):
        # new jobs run without it, so one account never scrapes and mails at once
        self.scraping = None
        self.in_job: list = []  # the workers of the job run() is running: no scrape starts on them
        self.polling = None  # the session path autoreply is polling now: no scrape starts on it either

    @property
    def workers(self) -> list:
        return list(self.storage.sessions)

    def count(self) -> int:
        return len(self.workers)

    @staticmethod
    def _assert_workers(sessions):
        for session in sessions:
            if not isinstance(session, TelegramClient):
                raise HostActionBlocked(
                    "risky actions are delegated to worker accounts only, never the host"
                )

    def busy(self, path: str) -> bool:
        """The worker (session path) is in the job run() is running, or autoreply polls it."""
        return path == self.polling or any(self.storage.get_session_path(worker) == path for worker in self.in_job)

    def delegate(self, func_instance, only=None) -> list:
        """Point a function at the workers (never the host) but the one the scraper runs
        on, which is put on hold; returns the worker list. `only` (session paths) narrows
        it to the workers the operator picked."""
        workers, on_hold = self.workers, []
        if only is not None:
            workers = [w for w in workers if self.storage.get_session_path(w) in only]
        if self.scraping is not None:
            on_hold = [w for w in workers if self.storage.get_session_path(w) == self.scraping.path]
            workers = [w for w in workers if w not in on_hold]
        self._assert_workers(workers)
        func_instance.sessions = workers
        func_instance.on_hold = on_hold  # a mailing keeps its people for it (see split_queues)
        return workers

    async def run(self, func_instance, bot_function: BotFunction, coro_factory, report, only=None) -> bool:
        """Delegate a job to the workers (`only`: the picked ones, see delegate); False if it
        was not run at all.

        coro_factory(func_instance) returns the awaitable to run (the function's run()).
        """
        workers = self.delegate(func_instance, only)
        scraping = self.scraping if func_instance.on_hold else None  # the worker left out

        if only is not None and not workers:
            await report(
                "⚠️ Выбранный воркер занят скрапом — дождитесь его окончания." if scraping else
                "⚠️ Выбранные воркеры недоступны. Начните заново."
            )
            return False

        if bot_function.risk == RISKY and not workers:
            await report(
                "⚠️ Единственный воркер занят скрапом — дождитесь его окончания." if scraping else
                "⚠️ Нет воркер-аккаунтов. Рискованные задачи выполняются только через добавленные аккаунты."
            )
            return False

        self.in_job = workers  # before any await: a scrape must not start on them meanwhile
        try:
            if scraping is not None:
                await report(f"ℹ️ Воркер {scraping.label} занят скрапом — задача идёт без него.")
            await coro_factory(func_instance)
        finally:
            self.in_job = []
        return True
