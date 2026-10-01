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

    def delegate(self, func_instance) -> list:
        """Point a function at the workers (never the host); returns the worker list."""
        workers = self.workers
        self._assert_workers(workers)
        func_instance.sessions = workers
        return workers

    async def run(self, func_instance, bot_function: BotFunction, coro_factory, report):
        """Delegate a job to the workers.

        coro_factory(func_instance) returns the awaitable to run (the function's run()).
        """
        if bot_function.risk == RISKY and not self.workers:
            await report(
                "⚠️ Нет воркер-аккаунтов. Рискованные задачи выполняются только через добавленные аккаунты."
            )
            return

        self.delegate(func_instance)
        await coro_factory(func_instance)
