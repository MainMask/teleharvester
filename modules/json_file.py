"""Small JSON state files (stats/...): read with a default, written atomically."""
import json
import os

from modules import atomic_file


def load(path: str, default):
    """The file's data; `default` if there is no file."""
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fileobj:
        return json.load(fileobj)


def save(path: str, data):
    """Written whole or not at all (modules/atomic_file.py)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    # dumps, not dump: only the one-shot call uses the C encoder (~4x faster), and a big
    # ledger (stats/pm_mailing.json) is rewritten on the event loop
    atomic_file.write(path, json.dumps(data, ensure_ascii=False))
