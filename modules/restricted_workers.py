"""Workers the last status check found permanently restricted: session file paths
in stats/restricted.json.

They stay in their project's sessions/, but the mailing and adding to contacts leave them out, so
their people go to other workers. Each status check rewrites the list: a worker found
clean again is back in the next run.

The last check of every other worker: {session file path: "active" or the date as
@SpamBot wrote it} in stats/spambot_status.json, for the bot's accounts list; rewritten
the same way.

Permanently restricted workers whose contacts the admin gave to the other workers (the
button after a status check): session file paths in stats/released.json. Until then a
permanently restricted worker's people wait for it: a misread @SpamBot reply must not hand
them to strangers.

Why the status check found a worker dead: {its file's path in sessions/inactive/: BANNED or
LOGGED_OUT} in stats/dead.json, for 📋 Список's 💀 Мёртвые. A dead one found before this was kept
has no entry: its cause is unknown.
"""
import os

from modules import json_file

RESTRICTED_PATH = os.path.join("stats", "restricted.json")
STATUS_PATH = os.path.join("stats", "spambot_status.json")
RELEASED_PATH = os.path.join("stats", "released.json")
DEAD_PATH = os.path.join("stats", "dead.json")

# a dead worker's cause: the account deleted or banned by Telegram; or alive but this session logged out
# (ended from another device, expired, its key dropped): only a new login brings it back
BANNED, LOGGED_OUT = "banned", "logged_out"
CAUSES = {BANNED: "бан", LOGGED_OUT: "вылет"}


def load() -> list:
    return json_file.load(RESTRICTED_PATH, [])


def save(paths: list):
    json_file.save(RESTRICTED_PATH, paths)


def load_status() -> dict:
    return json_file.load(STATUS_PATH, {})


def save_status(status: dict):
    json_file.save(STATUS_PATH, status)


def load_released() -> list:
    return json_file.load(RELEASED_PATH, [])


def save_released(paths: list):
    json_file.save(RELEASED_PATH, paths)


def load_dead() -> dict:
    return json_file.load(DEAD_PATH, {})


def save_dead(causes: dict):
    json_file.save(DEAD_PATH, causes)


def classify(path, forever: set, status: dict) -> tuple[int, str]:
    """The last @SpamBot check's result for the accounts list: (group, label); the list goes
    working first (0), then restricted until a date (1), permanently restricted last (2)."""
    if path in forever:
        return 2, "⛔ ограничен бессрочно"
    if path not in status:
        return 0, "❔ не проверялся"
    if status[path] == "active":
        return 0, "✅ без ограничений"
    return 1, f"🚫 ЛС ограничены до {status[path]}"


def _replace(old: str, new: str | None):
    """A worker's file `old` now at `new` (None: gone) in every store: its state goes with it."""
    def moved(paths: list) -> list:
        return sorted(new if path == old else path for path in paths if new is not None or path != old)

    if old in (restricted := load()):
        save(moved(restricted))
    if old in (released := load_released()):
        save_released(moved(released))
    for load_dict, save_dict in ((load_status, save_status), (load_dead, save_dead)):
        if old in (found := load_dict()):
            value = found.pop(old)
            if new is not None:
                found[new] = value
            save_dict(found)


def rename(old: str, new: str):
    """A worker's file moved to another project (projects.move_account): its state goes with it."""
    _replace(old, new)


def forget(path: str):
    """A worker's file deleted: its state goes too."""
    _replace(path, None)


def release(path: str):
    """The admin's decision: the worker's people go to the other workers from the next run."""
    released = load_released()
    if path not in released:
        save_released(sorted(released + [path]))
