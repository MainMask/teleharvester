import asyncio

from rich.markup import escape
from rich.table import Table
from modules.console import console

from functions.base import TelethonFunction
from modules import login as sign_in, restricted_workers
from modules.scraped_files import unfinished_scrape
from modules.scraper_creds import personal_storage, scrape_accounts
from modules.storages.sessions_storage import profile_order


def print_personal(listed) -> None:
    """The personal accounts, as stored: one is never connected just to be listed."""
    console.print(f"\n[bold white]Личные (только для скрапа): {len(listed)}[/]")
    for index, account in enumerate(listed, 1):
        console.print(f"[{index}] {account.label}  {account.path}", markup=False, highlight=False)


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
        await self.list_workers()
        if listed := scrape_accounts(None, personal_storage(self.settings.api_id, self.settings.api_hash)):
            print_personal(listed)

    async def list_workers(self):
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


class RemovePersonalFunc(TelethonFunction):
    """Remove a personal account"""

    async def execute(self):
        personal = personal_storage(self.settings.api_id, self.settings.api_hash)
        listed = scrape_accounts(None, personal)
        if not listed:
            console.print("[bold red]Личных аккаунтов нет (personal_sessions/).[/]")
            return
        print_personal(listed)
        raw = console.input("[bold white]какой убрать> [/]").strip()
        if not raw.isdigit() or not 1 <= int(raw) <= len(listed):
            console.print("[bold red]Нет такого номера.[/]")
            return
        account = listed[int(raw) - 1]

        if unfinished_scrape(account.path):  # a scrape continues on the account it was started on only
            console.print("⛔ На этом аккаунте есть незавершённый скрап — продолжить его можно только на нём.",
                          markup=False, highlight=False)
            return
        console.print(f"Будет завершена авторизация из файла {account.path} ({account.label}) и удалён сам файл. "
                      "Вход на телефоне не затрагивается; но если файл получен из tdata Telegram Desktop, "
                      "тот Desktop тоже выйдет из аккаунта.", markup=False, highlight=False)
        if console.input("[bold white]Убрать? (y/n) [/]").strip() != "y":
            console.print("Отменено.")
            return

        logged_out = await sign_in.remove_personal(personal, account)
        console.print(
            f"✅ Личный аккаунт {account.label} убран: сессия завершена, файл удалён." if logged_out else
            f"⚠️ Файл личного аккаунта {account.label} удалён, но завершить сессию не удалось — "
            "завершите её вручную: Telegram → Настройки → Устройства.",
            markup=False, highlight=False,
        )
