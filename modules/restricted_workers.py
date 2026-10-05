"""Workers the last status check found permanently restricted: session file paths
in stats/restricted.json.

They stay in sessions/, but the mailing and adding to contacts leave them out, so
their people go to other workers. Each status check rewrites the list: a worker found
clean again is back in the next run.
"""
import os

from modules import json_file

RESTRICTED_PATH = os.path.join("stats", "restricted.json")


def load() -> list:
    return json_file.load(RESTRICTED_PATH, [])


def save(paths: list):
    json_file.save(RESTRICTED_PATH, paths)
