import asyncio
import dataclasses
import multiprocessing
import os
import queue
import signal
import time
from dataclasses import dataclass
from typing import Any, Callable

from scraper.members import MembersParams, run as members_run
from scraper.scrape import ScrapeParams, run as scrape_run
from scraper.verify import VerifyParams, run as verify_run

TELEGRAM_UPLOAD_LIMIT = 50 * 1024 * 1024  # bot send_document limit


def dir_snapshot(path) -> dict:
    """{name: mtime_ns}: a re-run overwrites same-named files, so compare mtimes too."""
    if not os.path.isdir(path):
        return {}
    snapshot = {}
    for name in os.listdir(path):
        try:
            snapshot[name] = os.stat(os.path.join(path, name)).st_mtime_ns
        except OSError:  # a dangling symlink / a file removed meanwhile: not an output file
            pass
    return snapshot


def new_files(path, before: dict) -> list[str]:
    after = dir_snapshot(path)
    paths = (os.path.join(path, name) for name, mtime in after.items() if before.get(name) != mtime)
    return sorted(p for p in paths if os.path.isfile(p))  # skip dirs (e.g. <name>_partial)


# The scraper's jobs run in a child process, not a thread of the bot: a giant scrape's final
# step holds hundreds of MB to GBs (pandas/pyarrow) that a long-lived process wouldn't give
# back to the OS, an out-of-memory kill takes only the job, and a stop or a shutdown works
# at any step. spawn: a fresh interpreter, none of the bot's memory, loop or sockets.
_CTX = multiprocessing.get_context("spawn")
CHILD_STOP_GRACE = 20  # seconds a stopped job gets on a shutdown (systemd's TimeoutStopSec is 30)
PROGRESS_EVERY = 0.5   # seconds: a job reports progress per post / member, the bot needs far less


class _Throttle:
    """on_progress across the process boundary: at most one message per PROGRESS_EVERY;
    a value held back meanwhile goes out with the next one due, or with flush()."""

    def __init__(self, send):
        self.send, self.sent_at, self.held = send, None, None

    def __call__(self, *args):
        now = time.monotonic()
        if self.sent_at is not None and now - self.sent_at < PROGRESS_EVERY:
            self.held = args
            return
        self.sent_at, self.held = now, None
        self.send(args)

    def flush(self):
        if self.held is not None:
            self.send(self.held)
            self.held = None


def _child(target, credentials, params, events, stop, report_progress):
    """The child process: run target(credentials, params), report back through `events`."""
    # systemd stops the whole cgroup with SIGTERM: end like a stop (a scrape checkpoints)
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    params.stop = stop  # a multiprocessing.Event: the same is_set() watch_stop polls
    progress = None
    if report_progress:
        progress = params.on_progress = _Throttle(lambda args: events.put(("progress", args)))
    try:
        outcome = ("done", "ok", target(credentials, params))
    except SystemExit as exc:
        outcome = ("done", "exit", exc.code)
    except BaseException as exc:
        outcome = ("done", "error", f"{type(exc).__name__}: {exc}")
    if progress is not None:
        progress.flush()  # the latest value before the result
    events.put(outcome)


async def _run_in_process(target, credentials, params):
    """target(credentials, params) in a child process, as if called here: its progress
    reaches params.on_progress, params.stop (set by the bot's ⏹) stops it, its SystemExit
    or error is raised here."""
    on_progress, stop = params.on_progress, params.stop
    params = dataclasses.replace(params, on_progress=None, stop=None)  # neither crosses processes

    events = _CTX.Queue()
    child_stop = _CTX.Event()
    process = _CTX.Process(target=_child, name=f"scraper-{target.__module__}",
                           args=(target, credentials, params, events, child_stop, on_progress is not None))
    process.start()
    try:
        while True:
            if stop is not None and stop.is_set():
                child_stop.set()
            try:
                message = await asyncio.to_thread(events.get, True, 0.5)
            except queue.Empty:
                if process.is_alive():
                    continue
                try:  # gone: its last message may still be on the way
                    message = await asyncio.to_thread(events.get, True, 1)
                except queue.Empty:
                    hint = " — вероятно, не хватило памяти" if process.exitcode == -signal.SIGKILL else ""
                    raise RuntimeError(f"процесс скрапера аварийно завершился (код {process.exitcode}{hint})")
            if message[0] == "progress":
                on_progress(*message[1])
                continue
            _, kind, value = message
            if kind == "exit":
                raise SystemExit(value)
            if kind == "error":
                raise RuntimeError(value)
            return value
    finally:
        # leaving while it runs (the bot shutting down, an error here): stop it, give it time
        # to end cleanly (a scrape checkpoints), then end it; off the loop, joins take a moment
        if process.is_alive():
            child_stop.set()
            await asyncio.to_thread(process.join, CHILD_STOP_GRACE)
            if process.is_alive():
                process.kill()  # not terminate(): the child's SIGTERM only sets the stop (see _child)
        await asyncio.to_thread(process.join)
        events.close()


async def do_scrape(credentials, params: ScrapeParams):
    return await _run_in_process(scrape_run, credentials, params)


async def do_members(credentials, params: MembersParams):
    return await _run_in_process(members_run, credentials, params)


async def do_verify(credentials, params: VerifyParams):
    return await _run_in_process(verify_run, credentials, params)


@dataclass
class AnalysisJob:
    tool: str
    path: Any  # a file, or for "combine" a list / folder / glob
    data: dict
    on_progress: Callable | None = None  # _run_in_process's slots; a tool reports none
    stop: Any = None


def _analysis(credentials, job: AnalysisJob):
    from bot.routers.scraping import run_tool  # in the child: the bot's tool table

    return run_tool(job.tool, job.path, job.data)


async def do_analysis(tool: str, path, data: dict):
    """An analysis tool's (text, file) — in a child process, like the scraper's jobs."""
    return await _run_in_process(_analysis, None, AnalysisJob(tool, path, data))
