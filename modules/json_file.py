"""Small JSON state files (stats/...): read with a default, written atomically."""
import json
import os


def load(path: str, default):
    """The file's data; `default` if there is no file."""
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fileobj:
        return json.load(fileobj)


def save(path: str, data):
    """Via a temp file + rename: a write that dies midway (or a power cut) leaves the old file whole."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fileobj:
        # dumps, not dump: only the one-shot call uses the C encoder (~4x faster), and a big
        # ledger (stats/pm_mailing.json) is rewritten on the event loop
        fileobj.write(json.dumps(data, ensure_ascii=False))
        fileobj.flush()
        os.fsync(fileobj.fileno())  # on disk before the rename: a power cut must not leave it empty

    os.replace(tmp_path, path)
