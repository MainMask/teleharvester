from rich.console import Console
from rich.prompt import Prompt

from scraper.config import Credentials

console = Console()


def build_credentials(settings, session_string: str) -> Credentials:
    """scraper Credentials from teleharvester's config.toml + a chosen account.

    session_string wins over any session file / .env in session_for(), so the
    scraper runs on the picked teleharvester account without a separate login."""
    return Credentials(
        api_id=settings.api_id,
        api_hash=settings.api_hash,
        session_string=session_string,
    )


def pick_session_string(storage) -> str | None:
    """Pick one teleharvester account and return its StringSession string.

    `client.session.save()` serialises the auth key without connecting, so no
    extra connection is opened here (the scraper builds its own client)."""
    sessions = storage.sessions

    if not sessions:
        console.print("[bold red]No accounts in sessions/. Add one first.[/]")
        return None

    for index, client in enumerate(sessions):
        path = storage.get_session_path(client)
        console.print(f"[bold white][{index + 1}] {path}[/]")

    choice = int(Prompt.ask("[bold magenta]account to use[/]", default="1")) - 1

    if choice < 0 or choice >= len(sessions):
        console.print("[bold red]Invalid account number.[/]")
        return None

    return sessions[choice].session.save()
