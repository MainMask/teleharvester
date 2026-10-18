import os

from rich.prompt import Prompt
from modules.console import console

from functions.base import TelethonFunction
from functions.spamblock import set_restricted_proxies
from modules import projects, restricted_workers, tdata_import
from modules.types.proxy import ACCOUNTS_PER_PROXY, overlap


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

        aside = projects.restricted_proxies_path(self.storage.home)
        spare_path = Prompt.ask(
            "[bold red]файл прокси для бессрочно ограниченных (без лимита на прокси; «-» — не отделять)[/]",
            default=aside,
        ).strip()
        if spare_path == "-":
            spare = []
        elif not os.path.exists(spare_path):
            if os.path.abspath(spare_path) != os.path.abspath(aside):  # a typo mustn't drop the list
                console.print(f"[bold red]Файл не найден: {spare_path}[/]")
                return
            spare = []  # never set: not separated
        else:
            try:
                spare = tdata_import.load_proxies(spare_path)
            except ValueError as err:
                console.print(f"[bold red]Ошибка в строке прокси:[/] {err}")
                return
            if not spare:
                console.print(f"[bold red]В {spare_path} нет прокси («-» — не отделять)[/]")
                return
        if overlap(spare, proxies):
            console.print("[bold red]Прокси для ограниченных не должны повторять основные[/]")
            return

        old = dict(self.storage.full_sessions)
        try:
            summary = self.storage.apply_proxies(proxies, spare, set(restricted_workers.load()))
        except ValueError as err:
            console.print(f"[bold red]{err}[/]")
            return
        await self.reconnect_rebuilt(old)

        console.print(
            f"[bold green]Прокси назначены аккаунтам: {summary['accounts']}[/] "
            f"(использовано прокси: {summary['proxies_used']})"
        )
        if summary["restricted"]:
            console.print(f"[bold white]Бессрочно ограниченных на своих прокси: {summary['restricted']}[/]")
        # as the bot's: the project's pool its phone logins and tdata imports take their proxies from
        pool = projects.proxies_path(self.storage.home)
        if os.path.abspath(path) != os.path.abspath(pool):
            with open(path, encoding="utf-8") as fileobj:
                tdata_import.save_proxies(fileobj.read(), pool)
            console.print(f"[bold white]Список сохранён в {pool}[/]")
        if spare_path == "-" and os.path.exists(aside):
            set_restricted_proxies(self.storage, None)
            console.print("[bold white]Ограниченные больше не отделяются: список для них удалён[/]")
        elif spare and os.path.abspath(spare_path) != os.path.abspath(aside):
            with open(spare_path, encoding="utf-8") as fileobj:  # as the pool's: the file's own lines
                set_restricted_proxies(self.storage, fileobj.read())
            console.print(f"[bold white]Список для ограниченных сохранён в {aside}[/]")
        if summary["string_sessions_skipped"]:
            console.print("[yellow]Пропущено .session-аккаунтов (без метаданных): "
                          f"{summary['string_sessions_skipped']}[/]")
