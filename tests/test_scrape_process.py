"""The bot runs the scraper's jobs in a child process (bot/services/scraping._run_in_process).

The targets below are module-level: the spawned child imports them from this module.
"""

import asyncio
import os
import signal
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import pytest

from bot.services import scraping


@dataclass
class _Params:
    value: Any = None
    on_progress: Callable | None = None
    stop: Any = None


def _returns_pid(creds, params):
    params.on_progress(1, 2)
    params.on_progress(2, 2)
    return (os.getpid(), params.value, creds)


def _exits(creds, params):
    raise SystemExit(params.value)


def _fails(creds, params):
    raise ValueError("bad channel")


def _waits_for_stop(creds, params):
    deadline = time.monotonic() + 30
    while not params.stop.is_set():
        if time.monotonic() > deadline:
            return "never stopped"
        time.sleep(0.05)
    return "stopped"


def _killed(creds, params):
    os.kill(os.getpid(), signal.SIGKILL)  # what the OOM killer does


def _ignores_stop(creds, params):
    params.on_progress()  # up: its SIGTERM handler is installed by now
    time.sleep(30)  # a long synchronous step (pandas) that never looks at the stop
    return "finished"


def _run(target, params):
    return asyncio.run(scraping._run_in_process(target, "creds", params))


def test_the_job_runs_in_another_process_and_reports_progress():
    seen = []
    pid, value, creds = _run(_returns_pid, _Params(value=42, on_progress=lambda *a: seen.append(a)))
    assert pid != os.getpid() and (value, creds) == (42, "creds")
    assert seen == [(1, 2), (2, 2)]


def test_a_system_exit_comes_back_as_is():
    with pytest.raises(SystemExit) as exc:
        _run(_exits, _Params(value="@x: channel not found"))
    assert exc.value.code == "@x: channel not found"


def test_an_error_comes_back_with_its_type():
    with pytest.raises(RuntimeError, match="ValueError: bad channel"):
        _run(_fails, _Params())


def test_the_bot_s_stop_reaches_the_child():
    stop = threading.Event()
    threading.Timer(0.5, stop.set).start()
    assert _run(_waits_for_stop, _Params(stop=stop)) == "stopped"


def test_a_killed_child_is_reported_not_waited_for():
    with pytest.raises(RuntimeError, match="код -9 — вероятно, не хватило памяти"):
        _run(_killed, _Params())


def test_a_bot_shutdown_stops_the_child():
    async def scenario():
        task = asyncio.create_task(scraping._run_in_process(_waits_for_stop, None, _Params()))
        await asyncio.sleep(1.5)  # the child is up and waiting
        task.cancel()  # what asyncio.run does to running handlers on a shutdown
        started = time.monotonic()
        with pytest.raises(asyncio.CancelledError):
            await task
        return time.monotonic() - started

    assert asyncio.run(scenario()) < 5  # stopped cleanly, well inside the grace period


def test_a_child_deaf_to_the_stop_is_killed_after_the_grace(monkeypatch):
    monkeypatch.setattr(scraping, "CHILD_STOP_GRACE", 0.5)

    async def scenario():
        up = asyncio.Event()
        task = asyncio.create_task(scraping._run_in_process(_ignores_stop, None, _Params(on_progress=up.set)))
        await asyncio.wait_for(up.wait(), 30)  # the child is inside its synchronous step
        task.cancel()
        started = time.monotonic()
        with pytest.raises(asyncio.CancelledError):
            await task
        return time.monotonic() - started

    assert asyncio.run(scenario()) < 5  # not the child's 30 s: its SIGTERM only sets the stop


def test_the_real_job_params_cross_to_the_child():
    import dataclasses
    import pickle
    from datetime import datetime, timezone
    from pathlib import Path

    from scraper.config import Credentials
    from scraper.members import MembersParams
    from scraper.scrape import ScrapeParams
    from scraper.verify import VerifyParams

    day = datetime(2024, 1, 1, tzinfo=timezone.utc)
    creds = Credentials(1, "h", "s", proxy=("socks5", "1.2.3.4", 1080), device={"device_model": "X"})
    for params in (ScrapeParams(["@a"], day, day, "n", out_dir=Path("out"), account="sessions/a.jsession"),
                   MembersParams(["@g"], "n"), VerifyParams("p.parquet", "@a", day, day)):
        params.on_progress, params.stop = (lambda *a: None), threading.Event()  # set by the bot
        pickle.dumps((creds, dataclasses.replace(params, on_progress=None, stop=None)))


# --- the bot's analysis tools run in a child process too ------------------------------------

def _posts_file(tmp_path, rows):
    import json

    import pandas as pd

    path = tmp_path / "x_posts.parquet"
    comment = {"Type": "comment", "Comment Author ID": 7, "Comment Content": "hi"}
    pd.DataFrame({"Group": ["@g"] * rows, "Message ID": [str(i) for i in range(rows)],
                  "Url": ["u"] * rows, "Date": ["2024-06-06 10:00:00"] * rows,
                  "Comments List": [json.dumps([comment])] * rows}).to_parquet(path)
    return str(path)


def test_an_analysis_tool_runs_in_a_child_process(tmp_path):
    import conftest

    path = _posts_file(tmp_path, 25)
    text, out = asyncio.run(conftest.REAL_DO_ANALYSIS("comments", path, {}))
    assert text is None and os.path.exists(out)  # the tool's file, written by the child

    preview, out = asyncio.run(conftest.REAL_DO_ANALYSIS("read", path, {}))
    assert out is None and "[25 строк x 5 столбцов]" in preview  # head read, the file's row count


def test_an_analysis_input_error_comes_back_as_system_exit(tmp_path):
    import conftest

    with pytest.raises(SystemExit):
        asyncio.run(conftest.REAL_DO_ANALYSIS("combine", str(tmp_path / "nothing_*.parquet"), {}))


def test_progress_is_throttled_but_the_latest_value_arrives(monkeypatch):
    sent = []
    monkeypatch.setattr(scraping, "PROGRESS_EVERY", 60)
    throttle = scraping._Throttle(sent.append)
    for n in range(1, 101):  # a run's per-post calls
        throttle(n, 100)
    assert sent == [(1, 100)]  # the first goes out at once, the rest is held
    throttle.flush()
    assert sent == [(1, 100), (100, 100)]
