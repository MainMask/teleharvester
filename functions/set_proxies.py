import os

from rich.prompt import Prompt
from modules.console import console

from functions.base import TelethonFunction
from modules import projects, tdata_import
from modules.types.proxy import ACCOUNTS_PER_PROXY


class SetProxiesFunc(TelethonFunction):
    """Set proxies"""

    async def execute(self):
        path = Prompt.ask(
            f"[bold red]файл прокси (по одному scheme://user:pass@ip:port на строку; "
            f"{ACCOUNTS_PER_PROXY} аккаунта на прокси)[/]",
            default=projects.proxies_path(self.storage.home),
        )
        try:
            proxies = tdata_import.load_proxies(path)
        except ValueError as err:
            console.print(f"[bold red]Ошибка в строке прокси:[/] {err}")
            return
        if not proxies:
            console.print(f"[bold red]В {path} нет прокси[/]")
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
            f"[bold green]Прокси назначены аккаунтам: {summary['accounts']}[/] "
            f"(использовано прокси: {summary['proxies_used']})"
        )
        # as the bot's: the project's pool its phone logins and tdata imports take their proxies from
        pool = projects.proxies_path(self.storage.home)
        if os.path.abspath(path) != os.path.abspath(pool):
            with open(path, encoding="utf-8") as fileobj:
                tdata_import.save_proxies(fileobj.read(), pool)
            console.print(f"[bold white]Список сохранён в {pool}[/]")
        if summary["string_sessions_skipped"]:
            console.print("[yellow]Пропущено .session-аккаунтов (без метаданных): "
                          f"{summary['string_sessions_skipped']}[/]")
