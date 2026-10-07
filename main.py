import asyncio
import locale
import sys

from modules.console import console
from rich.markup import escape

from bot.services.registry import RISK_NOTE, RISKY, cli_menu

from modules import instance_lock, updater
from modules.config import load_env
from modules.settings import Settings
from modules.storages.functions_storage import FunctionsStorage
from modules.storages.sessions_storage import SessionsStorage
from modules.tdata_import import import_all


def menu_entries(functions) -> list:
    """[(section, instance, title, hint, risky)] in the bot's order and words; a function
    the registry doesn't know goes to «Прочее» under its docstring."""
    by_class = {type(instance).__name__: (instance, doc) for instance, doc in functions}
    entries = []
    for section, items in cli_menu():
        for item in items:
            if item.classname in by_class:
                instance, _ = by_class.pop(item.classname)
                entries.append((section, instance, item.title, item.hint, item.risk == RISKY))
    for instance, doc in by_class.values():
        entries.append(("Прочее", instance, doc or type(instance).__name__, "", False))
    return entries


def print_menu(entries) -> None:
    section = None
    for number, (entry_section, _, title, hint, risky) in enumerate(entries, 1):
        if entry_section != section:
            section = entry_section
            console.print(f"\n[bold magenta]{escape(section)}[/]")
        marker = "⚠️ " if risky else ""
        line = f"  \\[{number}] {marker}[bold white]{escape(title)}[/]"
        console.print(line + (f" — {escape(hint)}" if hint else ""))
    console.print(f"\n[italic]{escape(RISK_NOTE)}[/]")


def main() -> None:
    if "utf-8" not in (locale.getlocale()[1] or "").lower():
        console.print("[bold yellow]ВНИМАНИЕ:[/] кодировка терминала не UTF-8 — teleharvester может работать некорректно")

    # not while the bot runs: the same worker sessions and stats/; before the update check,
    # so a `git pull` + `pip install` never runs under a live bot
    instance_lock.hold()

    with console.status("Проверка обновлений..."):
        update = updater.check_update()

    if update["has_update"]:
        current_commit = update["current_commit"]
        upcoming_commit = update["upcoming_commit"]
        message = update["message"]

        console.print("[bold white]Вышло обновление teleharvester.[/]")

        console.print(
            "[yellow]{current_commit}[/] → [green]{upcoming_commit}[/] : [white]{message}[/]"
            .format(current_commit=current_commit[:8] if current_commit else "неизвестно", upcoming_commit=upcoming_commit[:8], message=message)
        )

        install_choice = console.input("[bold white]Установить? (y/n) >> [/]")

        if install_choice == "y":
            updater.update(console)

    else:
        console.print("У вас последняя версия teleharvester :)")

    if sys.version_info < (3, 11, 0):
        console.print("\n[red]Ошибка: устаревшая версия Python. Нужен Python 3.11.0 или новее.")
        return

    if sys.platform == "win32":
        console.print("[yellow]Внимание: на Windows некоторые функции могут работать некорректно\n")

    load_env()
    Settings.ensure_config()
    settings = Settings()

    try:
        with console.status("Импорт воркеров из tdata..."):
            imported = asyncio.run(import_all())
        if imported:
            console.print(f"[bold green]Импортировано аккаунтов из tdata_import/: {imported}[/]")
    except Exception as err:
        console.print(f"[bold yellow]ВНИМАНИЕ:[/] импорт tdata не удался: {err}")

    initialize = console.input("Подключить сессии сейчас? (y/n) ") == "y"

    sessions_storage = SessionsStorage(
        "sessions",
        settings.api_id,
        settings.api_hash,
        initialize=initialize
    )

    functions_storage = FunctionsStorage(
        "functions",
        sessions_storage,
        settings
    )

    console.print("[bold white]аккаунтов: %d[/]" % len(sessions_storage))

    entries = menu_entries(functions_storage.functions)
    print_menu(entries)

    while True:
        console.print()

        try:
            choice = console.input(
                "[bold white]>> [/]"
            )

            while not choice.isdigit():
                choice = console.input(
                    "[bold white]>> [/]"
                )
        except (KeyboardInterrupt, EOFError):
            console.print("[bold white]Пока![/]")
            break

        else:
            choice = int(choice) - 1

        if choice < 0 or choice >= len(entries):
            console.print("[bold red]нет такого пункта[/]")
            continue

        try:
            functions_storage.run_instance(entries[choice][1])
        except KeyboardInterrupt:
            pass
        except Exception as err:
            console.print(f"[bold red]Ошибка:[/] {err}")


if __name__ == "__main__":
    main()
