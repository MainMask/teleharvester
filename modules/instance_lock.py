"""One teleharvester process per folder: the terminal menu and the bot drive the same worker
sessions (one auth key from two processes) and rewrite the same stats/ files, and a bot start
sweeps tmp/ (the running one's broadcast media). The OS drops the lock on any exit, a crash too."""
import os
import sys

try:
    import fcntl
except ImportError:  # Windows: no flock; the bot runs on Linux (deploy/)
    fcntl = None

LOCK_PATH = os.path.join("tmp", "teleharvester.lock")

_held = None  # the locked file's descriptor, open for the whole process life


def hold():
    """Take the folder's lock, or exit if another teleharvester process holds it."""
    global _held
    if fcntl is None:
        return

    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    # read-only: flock needs no write access, so a file left by another user (`sudo python
    # main.py` once) doesn't stop the bot's start
    fd = os.open(LOCK_PATH, os.O_RDONLY | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        sys.exit("teleharvester уже запущен в этой папке (бот или терминальное меню). "
                 "Сначала остановите его, например `systemctl stop teleharvester-bot`: вместе они "
                 "использовали бы одни и те же сессии воркеров и перезаписывали бы stats/ друг друга.")
    _held = fd
