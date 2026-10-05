from rich.prompt import Prompt
from modules.console import console

from functions.base import TelethonFunction
from modules import tdata_import
from modules.types.proxy import ACCOUNTS_PER_PROXY


class SetProxiesFunc(TelethonFunction):
    """Set proxies"""

    async def execute(self):
        path = Prompt.ask(
            f"[bold red]proxies file (one scheme://user:pass@ip:port per line; "
            f"{ACCOUNTS_PER_PROXY} accounts per proxy)[/]",
            default=tdata_import.PROXIES_FILE,
        )
        try:
            proxies = tdata_import.load_proxies(path)
        except ValueError as err:
            console.print(f"[bold red]Bad proxy line:[/] {err}")
            return
        if not proxies:
            console.print(f"[bold red]No proxies in {path}[/]")
            return

        # the CLI keeps clients connected: drop the old ones, connect the rebuilt ones
        old = dict(self.storage.full_sessions)
        try:
            summary = self.storage.apply_proxies(proxies)
        except ValueError as err:
            console.print(f"[bold red]{err}[/]")
            return

        for path_, client in old.items():
            if self.storage.full_sessions.get(path_) is not client:
                await client.disconnect()
                if self.storage.initialize:
                    await self.storage.check_session(self.storage.full_sessions[path_], path_)

        console.print(
            f"[bold green]Proxies set for {summary['accounts']} account(s)[/] "
            f"(proxies used: {summary['proxies_used']})"
        )
        if summary["string_sessions_skipped"]:
            console.print(f"[yellow].session accounts skipped (no metadata): {summary['string_sessions_skipped']}[/]")
