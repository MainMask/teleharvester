import os
import sys
import toml
from modules.console import console
from typing import List, Tuple

from modules.config import load_toml


class Settings:
    def __init__(self):
        config = load_toml("config.toml")

        if not config:
            # Not interactive here: the terminal menu runs ensure_config() first, so
            # only non-CLI entry points (the bot) reach this, and they get a clear
            # message instead of a setup wizard.
            console.print("[bold red]config.toml not found. Run `python main.py` once to create it.[/]")
            raise SystemExit(1)

        try:
            self.api_id: int = config["sessions"]["api_id"]
            self.api_hash: str = config["sessions"]["api_hash"]
            self.messages: List[str] = config["broadcast"]["messages"]
            self.messages_count: int = config["broadcast"]["messages_count"]
            self.trigger: str = config["broadcast"]["trigger"]
            self.delay: List[int] = config["broadcast"]["delay"]
        except KeyError as err:
            console.print(
                f"[bold red]config.toml is missing {err}. "
                "Delete it and run `python main.py` once to recreate it.[/]"
            )
            raise SystemExit(1)

        limits = config.get("limits", {})
        self.per_account_daily: int = limits.get("per_account_daily", 30)
        self.account_pause: List[int] = limits.get("account_pause", [30, 60])

    @staticmethod
    def ensure_config(path: str = "config.toml"):
        """CLI first-run: create config.toml interactively, then exit.

        Called only from the terminal menu; the bot never triggers the wizard.
        """
        if os.path.exists(path):
            return

        Settings.initial_setup()
        sys.exit()

    @staticmethod
    def save(
        api_id: int,
        api_hash: str,
        messages: List[str],
        delay: List[int],
        messages_count: int,
        trigger: str
    ):
        config = dict(
            sessions=dict(
                api_hash=api_hash,
                api_id=api_id
            ),
            broadcast=dict(
                messages=messages,
                delay=delay,
                messages_count=messages_count,
                trigger=trigger
            ),
            limits=dict(
                per_account_daily=30,
                account_pause=[30, 60]
            )
        )

        with open("config.toml", "w") as file:
            toml.dump(config, file)

    @staticmethod
    def initial_setup():
        console.print(
            "[bold yellow]Initial setup[/]",
            justify="center"
        )

        print()

        console.print(
            "[bold blue]Sessions[/]",
            justify="center"
        )

        print()
        api_id, api_hash = Settings.setup_sessions()

        console.print(
            "[bold blue]Broadcast[/]",
            justify="center"
        )

        print()
        messages, delay, trigger = Settings.setup_broadcast()

        Settings.save(
            api_id,
            api_hash,
            messages,
            delay,
            0,
            trigger
        )

    @staticmethod
    def setup_sessions() -> Tuple[int, str]:
        api_id = console.input("[bold white]Enter API ID: [/]")
        api_hash = console.input("[bold white]Enter API hash: [/]")

        return int(api_id), api_hash

    @staticmethod
    def setup_broadcast() -> Tuple[List[str], List[int], str]:
        console.print("[bold white]Enter messages[/]")

        messages = []

        while message := console.input("[bold white]>> [/]"):
            messages.append(message)

        print()

        while True:
            parts = console.input("[bold white]Sending delay (e.g. 1-3): [/]").split("-")
            if parts and all(part.strip().isdigit() for part in parts):
                delay = [int(part) for part in parts]
                break

        trigger = console.input("[bold white]Enter the trigger text after which accounts start the broadcast: [/]")

        return messages, delay, trigger


