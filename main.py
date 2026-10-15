import locale
import os
import sys

from modules.console import console
from rich.markup import escape

from modules import instance_lock, projects, updater
from modules.cli_menu import menu_entries, print_menu
from modules.config import load_env
from modules.settings import Settings
from modules.storages.functions_storage import FunctionsStorage
from modules.storages.sessions_storage import SessionsStorage


def main() -> None:
    if "utf-8" not in (locale.getlocale()[1] or "").lower():
        console.print("[bold yellow]ВНИМАНИЕ:[/] кодировка терминала не UTF-8 — teleharvester может работать "
                      "некорректно")

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
            .format(current_commit=current_commit[:8] if current_commit else "неизвестно",
                    upcoming_commit=upcoming_commit[:8], message=message)
        )

        install_choice = console.input("[bold white]Установить? (y/n) >> [/]")

        if install_choice == "y":
            updater.update(console)

    elif update.get("checked", True):  # a failed check has said so already
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
        projects.migrate()  # before anything reads a project: each needs its id (its folder)
    except ValueError as err:  # the projects' file can't be read: 📁 Проекты says so
        console.print(f"[bold yellow]ВНИМАНИЕ:[/] {escape(str(err))}")

    # the main menu's items work on no account: each project's card loads its own workers
    # (functions/projects.py)
    functions_storage = FunctionsStorage(
        "functions",
        SessionsStorage(os.devnull, settings.api_id, settings.api_hash, initialize=False),
        settings
    )

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
            console.print(f"[bold red]Ошибка:[/] {escape(str(err))}")


if __name__ == "__main__":
    main()
