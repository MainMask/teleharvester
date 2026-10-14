"""Add an account: the terminal menu's «➕ Добавить» (functions/sign_in.py), by the bot's
rules — by phone a worker or a personal account, no duplicates, a worker's proxy from the pool,
a personal account without a proxy and without its 2FA password; or the tdata_import/ folders."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(ROOT)
os.chdir(ROOT)  # sessions/, personal_sessions/, assets/ are relative to the root

from functions.sign_in import AddByPhoneFunc, run_standalone

if __name__ == "__main__":
    run_standalone(AddByPhoneFunc)
