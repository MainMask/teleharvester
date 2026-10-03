"""Single place that reads and parses config.toml.

Settings and the bot config go through load_toml so the file is parsed in one
way, in one spot. Secrets (API keys, bot token) live in .env instead: load_env
puts them into os.environ at the entry points.
"""

import os

import toml
from dotenv import find_dotenv, load_dotenv


def load_toml(path: str = "config.toml") -> dict:
    """Parse the TOML config; return {} if the file does not exist."""
    if not os.path.exists(path):
        return {}

    with open(path, encoding="utf-8") as file:
        return toml.load(file)


def load_env():
    """Load .env from the cwd (or a parent) into os.environ; real env vars win."""
    load_dotenv(find_dotenv(usecwd=True))
