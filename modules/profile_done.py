"""Which profile / security function last changed which worker, in stats/profile_done.json:
  "done":   {"<user id>": {function class name: "YYYY-MM-DD"}}
  "values": {function class name: {"<user id>": the value it set}} — a photo's file name, a bio
The worker pickers (bot and CLI) tick the workers a function has not run on yet, so the ones done
earlier are not changed a second time; "values" lets a random pick from a list or a folder avoid
what other workers have already got. A worker is its Telegram user id, not its session file: a
re-imported account under another file name is still the same worker. A StringSession worker
(no .jsession, no known id) is not tracked.

The ledger starts with every worker there is then marked done by every tracked function, date
unknown (""): they were set up before it was kept.
"""
import datetime
import os
import random
from collections import Counter

from modules import json_file, restricted_workers

PATH = os.path.join("stats", "profile_done.json")

TRACKED = (
    "ChangeNameFunc", "ChangeUsernameFunc", "ChangeBioFunc", "ChangeProfilePhotoFunc",
    "ClearPersonalChannelFunc", "HideLastSeenFunc", "SetPasswordFunc", "TerminateSessionsFunc",
)
SEEDED = ""  # done before the ledger was kept


def worker_key(storage, path: str | None) -> str | None:
    """The worker's Telegram user id from its .jsession, the ledger's key; None if unknown."""
    json_session = storage.jsessions_paths.get(path) if path is not None else None
    return str(json_session.account.account.user_id) if json_session is not None else None


def load() -> dict:
    return json_file.load(PATH, {"done": {}, "values": {}})


def seed(keys: list):
    """Start the ledger, if there is none yet, with these workers done by every tracked function."""
    if not os.path.exists(PATH):
        done = {key: dict.fromkeys(TRACKED, SEEDED) for key in keys if key is not None}
        json_file.save(PATH, {"done": done, "values": {}})


def mark(key: str | None, classname: str, value: str | None = None):
    """The function has just changed the worker (to `value`, if it picks one from a list)."""
    if key is None:
        return
    ledger = load()
    ledger["done"].setdefault(key, {})[classname] = datetime.date.today().isoformat()
    if value is not None:
        ledger["values"].setdefault(classname, {})[key] = value
    json_file.save(PATH, ledger)


def used(classname: str) -> list:
    """The values the function has set on the workers."""
    return list(load()["values"].get(classname, {}).values())


def pick_fresh(options: list, taken: list):
    """A random option among the ones least often in `taken`: one nobody has, while there is one."""
    counts = Counter(taken)
    least = min(counts[option] for option in options)
    return random.choice([option for option in options if counts[option] == least])


def notes(key: str | None, path: str, classname: str, ledger: dict, forever: set, status: dict) -> list[str]:
    """A picker's notes on a worker: when the function last changed it, its @SpamBot restriction."""
    result = []
    done = ledger["done"].get(key, {}).get(classname) if key is not None else None
    if done is not None:
        result.append("✓" if done == SEEDED else f"✓ {datetime.date.fromisoformat(done):%d.%m}")
    group, _ = restricted_workers.classify(path, forever, status)
    if group == 2:
        result.append("⛔")
    elif group == 1:
        result.append(f"🚫 до {status[path]}")
    return result
