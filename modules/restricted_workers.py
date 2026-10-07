"""Workers the last status check found permanently restricted: session file paths
in stats/restricted.json.

They stay in sessions/, but the mailing and adding to contacts leave them out, so
their people go to other workers. Each status check rewrites the list: a worker found
clean again is back in the next run.

The last check of every other worker: {session file path: "active" or the date as
@SpamBot wrote it} in stats/spambot_status.json, for the bot's accounts list; rewritten
the same way.

Permanently restricted workers whose contacts the admin gave to the other workers (the
button after a status check): session file paths in stats/released.json. Until then a
permanently restricted worker's people wait for it: a misread @SpamBot reply must not hand
them to strangers.
"""
import os

from modules import json_file

RESTRICTED_PATH = os.path.join("stats", "restricted.json")
STATUS_PATH = os.path.join("stats", "spambot_status.json")
RELEASED_PATH = os.path.join("stats", "released.json")


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


def release(path: str):
    """The admin's decision: the worker's people go to the other workers from the next run."""
    released = load_released()
    if path not in released:
        save_released(sorted(released + [path]))
