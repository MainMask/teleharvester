"""Files written whole or not at all: via a temp file, fsync'ed, then renamed over the old one, so
a write that dies midway (or a power cut) leaves the old file as it was. No project imports: the
scraper uses it too."""
import os
import stat


def commit(tmp, path):
    """`tmp` (written completely) takes `path`'s place: on disk before the rename, or a power cut
    could leave `path` empty."""
    with open(tmp, "rb") as fileobj:
        os.fsync(fileobj.fileno())
    os.replace(tmp, path)


def write(path, text: str, mode: int = 0o666):
    """`text` to `path`. Its mode is never wider than `mode`, nor than the file's own before: a
    rewrite must not open up a locked-down file (a session's auth key, a 2FA password)."""
    tmp = f"{path}.tmp"
    try:
        os.unlink(tmp)  # a leftover would keep its own mode: O_CREAT applies `mode` to a new file only
    except FileNotFoundError:
        pass
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode), "w", encoding="utf-8") as fileobj:
        fileobj.write(text)
    if os.path.exists(path):
        os.chmod(tmp, stat.S_IMODE(os.stat(path).st_mode) & mode)
    commit(tmp, path)
