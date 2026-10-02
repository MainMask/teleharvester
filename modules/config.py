"""Single place that reads and parses config.toml.

Settings, the bot config and the scraper's credential fallback all go through
load_toml so the file is parsed in one way, in one spot.
"""

import os

import toml


def load_toml(path: str = "config.toml") -> dict:
    """Parse the TOML config; return {} if the file does not exist."""
    if not os.path.exists(path):
        return {}

    with open(path, encoding="utf-8") as file:
        return toml.load(file)
