import asyncio

from rich.table import Table
from modules.console import console

from functions.base import TelethonFunction
from modules.storages.sessions_storage import profile_order


class AccountsFunc(TelethonFunction):
    """Accounts list"""

    def row(self, client, me) -> list:
        """Name, username, phone, proxy, status; stored data when the account didn't answer."""
        js = self.storage.jsessions_paths.get(self.storage.get_session_path(client))
        proxy = js.account.proxy if js is not None else None
        proxy = f"{proxy.proxy_type}://{proxy.ip}:{proxy.port}" if proxy else "—"

        if me is not None:
            name = " ".join(filter(None, [me.first_name, me.last_name])) or "—"
            phone = f"+{me.phone}" if me.phone else "—"  # get_me() may come back without it
            return [name, f"@{me.username}" if me.username else "—", phone, proxy, "ok"]
        if js is not None:
            account = js.account.account
            name = " ".join(filter(None, [account.first_name, account.last_name])) or "—"
            return [name, "—", f"+{account.phone_number}", proxy, "[red]no answer[/]"]
        return ["—", "—", "—", proxy, "[red]no answer[/]"]

    async def execute(self):
        workers = self.storage.sessions
        if not workers:
            console.print("[bold red]No accounts in sessions/[/]")
            return

        with console.status("Polling accounts..."):
            profiles = await asyncio.gather(*[self.storage.fetch_me(client) for client in workers])

        table = Table(title=f"Accounts: {len(workers)}")
        for column in ("#", "Name", "Username", "Phone", "Proxy", "Status"):
            table.add_column(column)

        ordered = sorted(zip(workers, profiles), key=lambda pair: profile_order(pair[1]))
        for index, (client, me) in enumerate(ordered, 1):
            table.add_row(str(index), *self.row(client, me))

        console.print(table)
