import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(ROOT)
os.chdir(ROOT)  # import_all's default paths (tdata_import/, sessions/, assets/) are relative to the root

from modules import instance_lock
from modules.console import console
from modules.tdata_import import import_all

if __name__ == "__main__":
    instance_lock.hold()  # not while the bot or the menu runs, as add_session.py / login.py
    count = asyncio.run(import_all())
    console.print(f"[bold white]Импортировано аккаунтов: {count}[/]")
