import os
from typing import NamedTuple

from modules.console import console
from modules.storages.sessions_storage import SessionsStorage
from rich.prompt import Prompt

from scraper.config import Credentials

# personal accounts (.jsession, as in sessions/): never workers, but a scrape — read-only — may
# run on one when it is picked explicitly
PERSONAL_DIR = "personal_sessions"


class ScrapeAccount(NamedTuple):
    path: str        # its session file: a resume runs on the same one
    client: object   # its TelegramClient (never connected here; build_credentials reads it)
    label: str
    personal: bool


def build_credentials(client) -> Credentials:
    """scraper Credentials for a teleharvester account: its key, app, proxy and device.

    The same auth key seen from another IP / app risks AUTH_KEY_DUPLICATED (revoked)."""
    init = client._init_request
    return Credentials(
        api_id=client.api_id,
        api_hash=client.api_hash,
        session_string=client.session.save(),
        proxy=client._proxy,
        # the same authorization must keep presenting the account's device, not Telethon's
        device={
            "device_model": init.device_model,
            "system_version": init.system_version,
            "app_version": init.app_version,
            "lang_code": init.lang_code,
            "system_lang_code": init.system_lang_code,
        },
    )


def personal_storage(api_id, api_hash) -> SessionsStorage | None:
    """The personal accounts, loaded like the workers (no connection); None without the folder."""
    if not os.path.isdir(PERSONAL_DIR):
        return None
    return SessionsStorage(PERSONAL_DIR, api_id, api_hash, initialize=False)


def _label(storage, path: str) -> str:
    json_session = storage.jsessions_paths.get(path)
    if json_session is None:  # a plain .session file: nothing but its name
        return os.path.basename(path)
    account = json_session.account.account
    phone = account.phone_number if account.phone_number.startswith("+") else f"+{account.phone_number}"
    handle = f"@{account.username}" if account.username else phone
    return f"{account.first_name} ({handle})" if account.first_name else handle


def scrape_accounts(workers, personal=None) -> list[ScrapeAccount]:
    """The accounts a scrape may run on: the personal ones first, then the workers."""
    accounts = []
    for storage, is_personal in ((personal, True), (workers, False)):
        if storage is None:
            continue
        for client in storage.sessions:
            path = storage.get_session_path(client)
            accounts.append(ScrapeAccount(path, client, _label(storage, path), is_personal))
    return accounts


def find_account(accounts: list[ScrapeAccount], path: str) -> ScrapeAccount | None:
    return next((account for account in accounts if account.path == path), None)


def pick_session(storage, personal=None) -> ScrapeAccount | None:
    """Pick the account to scrape with (personal ones included); None if none / bad input."""
    accounts = scrape_accounts(storage, personal)

    if not accounts:
        console.print("[bold red]No accounts in sessions/. Add one first.[/]")
        return None

    for index, account in enumerate(accounts):
        mark = " - personal" if account.personal else ""
        # no markup: a name could hold "[...]"
        console.print(f"[{index + 1}] {account.label}{mark}  {account.path}", markup=False,
                      highlight=False, style="bold white")

    raw = Prompt.ask("[bold magenta]account to use[/]", default="1")

    if not raw.isdigit():
        console.print("[bold red]Invalid account number.[/]")
        return None

    choice = int(raw) - 1

    if choice < 0 or choice >= len(accounts):
        console.print("[bold red]Invalid account number.[/]")
        return None

    return accounts[choice]
