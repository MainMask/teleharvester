"""The worker account a scrape runs on (built by modules.scraper_creds.build_credentials)."""

from dataclasses import dataclass


@dataclass
class Credentials:
    api_id: int
    api_hash: str
    session_string: str  # the worker's auth key, used by the scrape's own client
    proxy: tuple | dict | None = None  # the account's own proxy (.jsession)
    device: dict | None = None  # its device_model / app_version / ... (TelegramClient kwargs)
