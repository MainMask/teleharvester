"""Who added whom to contacts: {"<user_id>": <worker user_id>} in stats/contacts.json.

Each person is added to one worker's contacts; the mailing then routes them to that worker.
"""
import os

from modules import json_file

LEDGER_PATH = os.path.join("stats", "contacts.json")


def load() -> dict:
    return json_file.load(LEDGER_PATH, {})


def save(ledger: dict):
    json_file.save(LEDGER_PATH, ledger)
