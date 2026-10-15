"""Who added whom to contacts: {"<user_id>": <worker user_id>} in the project's contacts.json.

Each person is added to one worker's contacts; the mailing then routes them to that worker.
A project keeps its own: its workers are its own (modules/projects.py).
"""
import os

from modules import json_file

NAME = "contacts.json"  # in the project's folder (projects.home)


def load(home: str) -> dict:
    return json_file.load(os.path.join(home, NAME), {})


def save(home: str, ledger: dict):
    json_file.save(os.path.join(home, NAME), ledger)
