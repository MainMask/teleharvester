import os
import sys
import toml
from dotenv import set_key
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

        if "sessions" in config:
            console.print(
                "[bold red]API credentials moved to .env: put \\[sessions] api_id/api_hash into "
                "TG_API_ID/TG_API_HASH and delete the \\[sessions] section from config.toml.[/]"
            )
            raise SystemExit(1)

        api_id = os.environ.get("TG_API_ID", "")
        self.api_hash: str = os.environ.get("TG_API_HASH", "")

        if not api_id.isdigit() or not self.api_hash:
            console.print("[bold red]Set TG_API_ID and TG_API_HASH in .env (see .env.example).[/]")
            raise SystemExit(1)

        self.api_id: int = int(api_id)

        try:
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
        """CLI first-run: create config.toml (and the .env API keys) interactively, then exit.

        Called only from the terminal menu; the bot never triggers the wizard.
        """
        if os.path.exists(path):
            return

        Settings.initial_setup()
        sys.exit()

    @staticmethod
    def save(
        messages: List[str],
        delay: List[int],
        messages_count: int,
        trigger: str
    ):
        config = dict(
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

        with open("config.toml", "w", encoding="utf-8") as file:
            toml.dump(config, file)

    @staticmethod
    def initial_setup():
        console.print(
            "[bold yellow]Initial setup[/]",
            justify="center"
        )

        print()

        if not os.environ.get("TG_API_ID") or not os.environ.get("TG_API_HASH"):
            console.print(
                "[bold blue]Sessions[/]",
                justify="center"
            )

            print()
            api_id, api_hash = Settings.setup_sessions()

            # secrets go to .env, not config.toml
            set_key(".env", "TG_API_ID", str(api_id))
            set_key(".env", "TG_API_HASH", api_hash)

        console.print(
            "[bold blue]Broadcast[/]",
            justify="center"
        )

        print()
        messages, delay, trigger = Settings.setup_broadcast()

        Settings.save(
            messages,
            delay,
            0,
            trigger
        )

    @staticmethod
    def setup_sessions() -> Tuple[int, str]:
        api_id = console.input("[bold white]Enter API ID: [/]")

        while not api_id.isdigit():
            api_id = console.input("[bold white]API ID must be a number: [/]")

        api_hash = console.input("[bold white]Enter API hash: [/]")

        return int(api_id), api_hash

    @staticmethod
    def setup_broadcast() -> Tuple[List[str], List[int], str]:
        console.print("[bold white]Enter messages[/]")

        messages = []

        while (message := console.input("[bold white]>> [/]")) or not messages:
            if message:  # at least one message: the CLI broadcast picks from this list
                messages.append(message)

        print()

        while True:
            parts = console.input("[bold white]Sending delay (e.g. 1-3): [/]").split("-")
            if parts and all(part.strip().isdigit() for part in parts):
                delay = [int(part) for part in parts]
                break

        trigger = console.input("[bold white]Enter the trigger text after which accounts start the broadcast: [/]")

        return messages, delay, trigger


