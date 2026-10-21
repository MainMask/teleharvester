import asyncio
import itertools

from bot.keyboards.common import stop_kb
from bot.keyboards.menu import back_kb
from bot.services.progress import Progress
from bot.services.runner import TelegramReporter
from modules.storages.sessions_storage import release_client


def busy(label: str) -> str:
    """Why a job can't start now: another one holds the slot."""
    return f"⛔ Занято: идёт «{label}». Дождитесь окончания или остановите в 📊 Задачи."


class JobManager:
    """One active job at a time in this slot. Each project has three (bot/services/workspaces.py):
    `manager` for the workers' jobs, `scrapes` for the scraper's (scrape, members, verify), which run on their
    own client, so a multi-day scrape doesn't hold up the rest, and `analyses` for the analysis,
    which uses no account at all.

    Coro jobs (one-shot functions + the chat trigger-listener) run as a cancellable
    background task with a ⏹ Stop button. Interactive/threaded jobs (the report flow,
    the analysis, a tdata import) take their slot via acquire()/release().

    One slot for a project's workers' jobs is correct: every admin shares its worker pool, and the
    worker TelegramClient objects must not be driven by two jobs at once; projects have workers of
    their own, so their jobs run side by side.
    """

    _keys = itertools.count(1)

    def __init__(self):
        self.key = next(self._keys)  # what its 📊 and ⏹ buttons name it by (/status lists every project's)
        self.project_id = 0          # its project's (Workspace): a job's ⬅️ leads back into it
        self._active = False
        self._label = ""
        self._kind = None            # "task" | "interactive"
        self._task = None
        self._stop_sessions = []
        self._on_abort = None
        self._cancelable = True
        self._timeout_task = None
        self._cancel_requested = False
        self._soft_stop = None       # set by stop() on a locked job that stops by itself (soft_stop)
        self.progress = None         # Progress of the active job, for the 📊 button

    @property
    def active(self) -> bool:
        return self._active

    @property
    def label(self) -> str:
        return self._label

    @property
    def stoppable(self) -> bool:
        """⏹ would stop it now (stop()): a job not yet finishing, or a dialog that can be cancelled."""
        return self._active and (self._cancelable or self._soft_stop is not None)

    # --- coro jobs ---------------------------------------------------------

    async def run(self, bot, chat_id, pool, instance, bot_function, factory, header, done=None, stop_sessions=None,
                  cleanup=None, only=None) -> bool:
        """`done`: the summary of a job run to its end (by default "✅ Готово: <the function>"); `cleanup`
        (e.g. a captured broadcast's temp files): called once, when the job is over or when it doesn't start at
        all (False)."""
        done = done or f"✅ Готово: {bot_function.title}"
        # Take the slot synchronously (before any await) to avoid a race between tasks.
        if self._active:
            if cleanup is not None:
                cleanup()
            await bot.send_message(chat_id, busy(self._label),
                                   reply_markup=back_kb(bot_function.category, self.project_id))
            return False

        self._active = True
        self._label = bot_function.title  # as its button: in /status, in another job's busy()
        self._kind = "task"
        self._cancelable = True
        self.progress = Progress()

        reporter = TelegramReporter(
            bot, chat_id, header=header, reply_markup=stop_kb(self.key), job_label=bot_function.title,
            workers=len(pool.workers), final_markup=back_kb(bot_function.category, self.project_id),
            progress=self.progress,
        )
        try:
            await reporter.start()
        except Exception:
            # Couldn't even post the status message (chat unreachable): free the slot and
            # report "not started", its temp files dropped instead of leaking past a raise.
            self._clear()
            if cleanup is not None:
                cleanup()
            return False

        if self._cancel_requested:  # stopped while the status message was being posted
            self._clear()
            if cleanup is not None:
                cleanup()
            try:
                await reporter.finish("⏹ Остановлено")
            except Exception:
                pass
            return False

        # by default the job's own workers, once pool.run has given them (see _wrap.start): another
        # one may be in use meanwhile (the auto-reply, a login code) and must stay connected
        self._stop_sessions = list(stop_sessions) if stop_sessions is not None else []
        task = asyncio.create_task(
            self._wrap(pool, instance, bot_function, factory, reporter, done, cleanup, only, stop_sessions is None)
        )
        self._task = task
        # backstop: a cancel before _wrap starts, or during its finally, skips its _clear()
        task.add_done_callback(lambda t: self._clear() if self._task is t else None)
        return True

    async def _wrap(self, pool, instance, bot_function, factory, reporter, done, cleanup, only, own_workers):
        instance.progress = self.progress  # the function steps it (see BaseFunction.progress)

        def start(func):  # pool.run calls it once it has given the job its workers
            if own_workers:  # the ones it may connect: released at its end
                self._stop_sessions = list(func.sessions)
            reporter.workers = len(func.sessions)  # the summary's count: a scraping worker is left out
            return factory(func, reporter)

        try:
            ran = await pool.run(instance, bot_function, start, reporter, only=only)
            # done: a Stop during finish() must not relabel the job as stopped
            self._cancelable = False
            await reporter.finish("⚠️ Не выполнено" if ran is False else done)
        except asyncio.CancelledError:
            await reporter.finish("⏹ Остановлено")
        except Exception as err:  # noqa: BLE001 - surface any job failure to the operator
            self._cancelable = False  # as on success: a Stop must not swallow the error text
            await reporter.finish(f"⚠️ Ошибка: {err}")
        finally:
            if cleanup is not None:  # run to its end, failed, stopped, or not run (no free worker)
                cleanup()
            for session in self._stop_sessions:
                await release_client(session)  # memory stays bounded to one job's worth
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

    def soft_stop(self) -> asyncio.Event:
        """For a locked slot (see lock()) whose work can end between steps: stop() then sets
        the returned event instead of refusing, and the work skips what is left and calls
        release() itself. The slot stays held until then, so no job starts on its workers."""
        self._soft_stop = asyncio.Event()
        return self._soft_stop

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
            if self._soft_stop is not None:  # the locked work stops by itself (see soft_stop)
                self._soft_stop.set()
                return True
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
        self._soft_stop = None
        self.progress = None
