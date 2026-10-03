import asyncio

from bot.keyboards.common import stop_kb
from bot.services.runner import TelegramReporter


class JobManager:
    """One active job at a time across the whole bot.

    Coro jobs (one-shot functions + the chat trigger-listener) run as a cancellable
    background task with a ⏹ Stop button. Interactive/threaded jobs (the report flow,
    scraping) take the same single slot via acquire()/release().

    A single global slot is correct: every admin shares one worker pool, and the
    worker TelegramClient objects must not be driven by two jobs at once.
    """

    def __init__(self):
        self._active = False
        self._label = ""
        self._kind = None            # "task" | "interactive"
        self._task = None
        self._stop_sessions = []
        self._on_abort = None
        self._cancelable = True
        self._timeout_task = None
        self._cancel_requested = False

    @property
    def active(self) -> bool:
        return self._active

    @property
    def label(self) -> str:
        return self._label

    # --- coro jobs ---------------------------------------------------------

    async def run(self, bot, chat_id, pool, instance, bot_function, factory, header, done, stop_sessions=None) -> bool:
        # Take the slot synchronously (before any await) to avoid a race between tasks.
        if self._active:
            await bot.send_message(chat_id, f"⛔ Занят: {self._label}. Остановите текущую задачу.")
            return False

        self._active = True
        self._label = header
        self._kind = "task"
        self._cancelable = True

        reporter = TelegramReporter(bot, chat_id, header=header, reply_markup=stop_kb())
        try:
            await reporter.start()
        except Exception:
            # Couldn't even post the status message (chat unreachable): free the slot and
            # report "not started" so the caller runs its not-started cleanup (e.g. drops
            # the captured broadcast's temp files) instead of leaking them past a raise.
            self._clear()
            return False

        if self._cancel_requested:  # stopped while the status message was being posted
            self._clear()
            try:
                await reporter.finish("⏹ Остановлено")
            except Exception:
                pass
            return False

        self._stop_sessions = list(stop_sessions) if stop_sessions is not None else list(pool.workers)
        task = asyncio.create_task(
            self._wrap(pool, instance, bot_function, factory, reporter, done)
        )
        self._task = task
        # backstop: a cancel before _wrap starts, or during its finally, skips its _clear()
        task.add_done_callback(lambda t: self._clear() if self._task is t else None)
        return True

    async def _wrap(self, pool, instance, bot_function, factory, reporter, done):
        try:
            ran = await pool.run(instance, bot_function, lambda f: factory(f, reporter), reporter)
            # done: a Stop during finish() must not relabel the job as stopped
            self._cancelable = False
            await reporter.finish("⚠️ Не выполнено" if ran is False else done)
        except asyncio.CancelledError:
            await reporter.finish("⏹ Остановлено")
        except Exception as err:  # noqa: BLE001 - surface any job failure to the operator
            self._cancelable = False  # as on success: a Stop must not swallow the error text
            await reporter.finish(f"⚠️ Ошибка: {err}")
        finally:
            for session in self._stop_sessions:
                try:
                    await session.disconnect()
                except Exception:
                    pass
                # Telethon appends every RPC result's users/chats to the StringSession's
                # in-memory _entities set, which never shrinks; on a multi-day bot run the
                # persistent worker clients would grow unbounded. Drop it per job (functions
                # re-resolve peers anyway), keeping memory bounded to one job's worth.
                try:
                    session.session._entities.clear()
                except Exception:
                    pass
            self._clear()

    # --- interactive / threaded jobs --------------------------------------

    def acquire(self, label, on_abort=None, cancelable=True, timeout=600) -> bool:
        if self._active:
            return False

        self._active = True
        self._label = label
        self._kind = "interactive"
        self._on_abort = on_abort
        self._cancelable = cancelable

        if timeout:
            self._timeout_task = asyncio.create_task(self._expire(timeout))

        return True

    async def _expire(self, timeout):
        try:
            await asyncio.sleep(timeout)
        except asyncio.CancelledError:
            return
        if self._active and self._kind == "interactive":
            await self.stop()

    def disarm_timeout(self):
        """Cancel the inactivity timeout while keeping the slot held.

        Called once the operator's input is in and the (possibly long) work has
        started — e.g. the report flow's replay_rest — so the auto-timeout can't
        fire mid-work and free the slot out from under a running job.
        """
        if self._timeout_task is not None:
            self._timeout_task.cancel()
            self._timeout_task = None

    def lock(self):
        """Make the held interactive slot non-cancelable.

        Called alongside disarm_timeout once the long work has started (e.g. the
        report flow's replay_rest): a /cancel must not run on_abort and free the
        slot while that work is still driving the workers, or a second job could
        start on the same clients and _finish's release() would clear a slot that
        no longer belongs to it.
        """
        self._cancelable = False

    def release(self):
        self._clear()

    # --- stop / cleanup ----------------------------------------------------

    async def stop(self) -> bool:
        if not self._active:
            return False

        if self._kind == "task":
            if not self._cancelable:  # already finishing (see _wrap)
                return False
            if self._task is None:  # still posting the status message: run() aborts the start
                self._cancel_requested = True
            elif not self._task.cancelling():
                # a second cancel would interrupt _wrap's cleanup and skip its _clear()
                self._task.cancel()  # _wrap's finally clears the slot
            return True

        # interactive
        if not self._cancelable:
            return False

        on_abort = self._on_abort
        self._clear()
        if on_abort is not None:
            result = on_abort()
            if asyncio.iscoroutine(result):
                await result
        return True

    def _clear(self):
        # Don't cancel the timeout task if it's the one currently running _clear via stop().
        if self._timeout_task is not None and self._timeout_task is not asyncio.current_task():
            self._timeout_task.cancel()
        self._active = False
        self._label = ""
        self._kind = None
        self._task = None
        self._stop_sessions = []
        self._on_abort = None
        self._cancelable = True
        self._timeout_task = None
        self._cancel_requested = False
