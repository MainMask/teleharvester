import locale
import sys

from rich.console import Console

from modules import updater
from modules.settings import Settings
from modules.storages.functions_storage import FunctionsStorage
from modules.storages.sessions_storage import SessionsStorage

console = Console()

if "UTF-8" not in locale.getlocale():
    console.print("[bold yellow]WARNING:[/] You don't have UTF-8 encoding. teleharvester may not work")

with console.status("Checking updates..."):
    update = updater.check_update()

if update["has_update"]:
    current_commit = update["current_commit"]
    upcoming_commit = update["upcoming_commit"]
    message = update["message"]

    console.print("[bold white]A new teleharvester update has been released.[/]")

    console.print(
        "[yellow]{current_commit}[/] → [green]{upcoming_commit}[/] : [white]{message}[/]"
        .format(current_commit=current_commit[:8] if current_commit else "unknown", upcoming_commit=upcoming_commit[:8], message=message)
    )

    install_choice = console.input("[bold white]Install? (y/n) >> [/]")

    if install_choice == "y":
        updater.update(console)

else:
    console.print("You using the latest version of teleharvester :)")

if sys.version_info < (3, 8, 0):
    console.print("\n[red]Error: you using an outdated Python version. Install Python 3.8.0 at least.")
else:
    if sys.platform == "win32":
        console.print("[yellow]Warning: you using Windows. Some features may not work properly\n")

    settings = Settings()

    sessions_storage = SessionsStorage(
        "sessions",
        settings.api_id,
        settings.api_hash
    )

    functions_storage = FunctionsStorage(
        "functions",
        sessions_storage,
        settings
    )

    console.print("[bold white]accounts count> %d[/]" % len(sessions_storage))

    for index, module in enumerate(functions_storage.functions):
        instance, doc = module

        console.print(
            "[bold white][{index}] {doc}[/]"
            .format(index=index + 1, doc=doc)
        )

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
        except KeyboardInterrupt:
            console.print("[bold white]Bye![/]")
            break

        else:
            choice = int(choice) - 1

        if choice < 0:
            continue

        try:
            functions_storage.execute(choice)
        except KeyboardInterrupt:
            pass
        except Exception as err:
            console.print(f"[bold red]Error:[/] {err}")

