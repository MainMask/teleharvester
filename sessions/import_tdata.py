import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(ROOT)
os.chdir(ROOT)  # import_all's default paths (tdata_import/, sessions/, assets/) are relative to the root

from modules.console import console
from modules.tdata_import import import_all

if __name__ == "__main__":
    count = asyncio.run(import_all())
    console.print(f"[bold white]Imported {count} account(s)[/]")
