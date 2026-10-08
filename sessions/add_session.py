"""Add an account by phone: the terminal menu's «Добавить по номеру» (functions/sign_in.py),
by the bot's rules — a worker or a personal account, no duplicates, a worker's proxy from the pool,
a personal account without a proxy and without its 2FA password."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(ROOT)
os.chdir(ROOT)  # sessions/, personal_sessions/, assets/ are relative to the root

from functions.sign_in import AddByPhoneFunc, run_standalone

if __name__ == "__main__":
    run_standalone(AddByPhoneFunc)
