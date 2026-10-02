"""Load Telegram API credentials from the environment, a .env file, or teleharvester's config.toml."""

import os
from dataclasses import dataclass

from dotenv import find_dotenv, load_dotenv
from telethon.sessions import StringSession


@dataclass
class Credentials:
    api_id: int
    api_hash: str
    phone: str | None = None
    password: str | None = None
    session_string: str | None = None


def _toml_credentials():
    """api_id/api_hash from teleharvester's config.toml in the cwd, when no .env is set."""
    try:
        from modules.config import load_toml  # lazy: keeps scraper importable standalone
        s = load_toml("config.toml").get("sessions", {})
        return s.get("api_id"), s.get("api_hash")
    except Exception:
        return None, None


def load_credentials() -> Credentials:
    """Read TG_* variables. Real environment variables win over the .env file, which in
    turn wins over teleharvester's config.toml."""
    load_dotenv(find_dotenv(usecwd=True))  # the cwd (or a parent), not the package's install dir

    api_id = os.getenv("TG_API_ID")
    api_hash = os.getenv("TG_API_HASH")

    if not api_id or not api_hash:  # fall back to config.toml (teleharvester's own keys)
        t_id, t_hash = _toml_credentials()
        api_id = api_id or t_id
        api_hash = api_hash or t_hash

    missing = [name for name, value in (("TG_API_ID", api_id), ("TG_API_HASH", api_hash)) if not value]
    if missing:
        raise SystemExit(
            f"Missing credentials: {', '.join(missing)}. "
            "Copy .env.example to .env and fill it in (values from https://my.telegram.org/apps), "
            "or set them in config.toml under [sessions]."
        )

    try:
        api_id_int = int(api_id)
    except ValueError:
        raise SystemExit(f"TG_API_ID must be an integer, got {api_id!r}.")

    return Credentials(
        api_id=api_id_int,
        api_hash=api_hash,
        phone=os.getenv("TG_PHONE") or None,
        password=os.getenv("TG_PASSWORD") or None,
        session_string=os.getenv("TG_SESSION_STRING") or None,
    )


def start_kwargs(creds: Credentials) -> dict:
    """phone/password for client.start(); unset ones are left to Telethon's prompts
    (an explicit None makes start() raise instead of asking)."""
    return {k: v for k, v in (("phone", creds.phone), ("password", creds.password)) if v}


def session_for(creds: Credentials, session: str) -> StringSession | str:
    """TG_SESSION_STRING wins over the session file when it is set."""
    if not creds.session_string:
        return session
    try:
        return StringSession(creds.session_string)
    except ValueError:  # also binascii.Error (bad padding) from a truncated paste
        raise SystemExit("TG_SESSION_STRING is not a valid session string. "
                         "Copy it again from `scraper login --string`.")
