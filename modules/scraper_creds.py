from modules.console import console
from rich.prompt import Prompt

from scraper.config import Credentials


# resume hint for scrapes started from teleharvester (menu or bot): the CLI command
# scrape.run prints otherwise would resume on the .env account
TUI_RESUME_HINT = ("teleharvester menu -> Scrape channel/group: the same account, channels, name, "
                   "dates, keyword and output dir, then answer yes to 'resume an interrupted run?'")


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


def pick_session(storage):
    """Pick one teleharvester account; return its client (None if none / bad input)."""
    sessions = storage.sessions

    if not sessions:
        console.print("[bold red]No accounts in sessions/. Add one first.[/]")
        return None

    for index, client in enumerate(sessions):
        path = storage.get_session_path(client)
        console.print(f"[bold white][{index + 1}] {path}[/]")

    raw = Prompt.ask("[bold magenta]account to use[/]", default="1")

    if not raw.isdigit():
        console.print("[bold red]Invalid account number.[/]")
        return None

    choice = int(raw) - 1

    if choice < 0 or choice >= len(sessions):
        console.print("[bold red]Invalid account number.[/]")
        return None

    return sessions[choice]
