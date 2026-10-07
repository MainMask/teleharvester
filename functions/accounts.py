import asyncio

from rich.markup import escape
from rich.table import Table
from modules.console import console

from functions.base import TelethonFunction
from modules import restricted_workers
from modules.storages.sessions_storage import profile_order


class AccountsFunc(TelethonFunction):
    """Accounts list"""

    def row(self, client, me) -> list:
        """Name, username, phone, proxy, status; stored data when the account didn't answer."""
        js = self.storage.jsessions_paths.get(self.storage.get_session_path(client))
        proxy = js.account.proxy if js is not None else None
        proxy = f"{proxy.proxy_type}://{proxy.ip}:{proxy.port}" if proxy else "—"

        if me is not None:
            name = escape(" ".join(filter(None, [me.first_name, me.last_name]))) or "—"  # a cell is markup
            phone = f"+{me.phone}" if me.phone else "—"  # get_me() may come back without it
            return [name, f"@{me.username}" if me.username else "—", phone, proxy, "отвечает"]
        if js is not None:
            account = js.account.account
            name = escape(" ".join(filter(None, [account.first_name, account.last_name]))) or "—"
            return [name, "—", f"+{account.phone_number}", proxy, "[red]нет ответа[/]"]
        return ["—", "—", "—", proxy, "[red]нет ответа[/]"]

    async def execute(self):
        workers = self.storage.sessions
        if not workers:
            console.print("[bold red]Нет аккаунтов в sessions/[/]")
            return

        with console.status("Опрос аккаунтов..."):
            profiles = await asyncio.gather(*[self.storage.fetch_me(client) for client in workers])

        # the last @SpamBot check, as in the bot's list: restricted ones go last
        forever, status = set(restricted_workers.load()), restricted_workers.load_status()
        checks = {id(client): restricted_workers.classify(self.storage.get_session_path(client), forever, status)
                  for client in workers}

        title = f"Аккаунтов: {len(workers)}"
        if restricted := sum(1 for group, _ in checks.values() if group):
            title += f" · ограничены: {restricted}"
        table = Table(title=title)
        for column in ("#", "Имя", "Username", "Телефон", "Прокси", "Статус", "SpamBot"):
            table.add_column(column)

        ordered = sorted(zip(workers, profiles), key=lambda pair: (checks[id(pair[0])][0], profile_order(pair[1])))
        for index, (client, me) in enumerate(ordered, 1):
            table.add_row(str(index), *self.row(client, me), escape(checks[id(client)][1]))

        console.print(table)
