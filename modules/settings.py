import os
import sys
import toml
from rich.console import Console
from typing import List, Tuple

console = Console()


class Settings:
    def __init__(self):
        if not os.path.exists("config.toml"):
            self.initial_setup()
            sys.exit()

        with open("config.toml") as file:
            config = toml.load(file)

        self.api_id: int = config["sessions"]["api_id"]
        self.api_hash: str = config["sessions"]["api_hash"]
        self.messages: List[str] = config["broadcast"]["messages"]
        self.messages_count: int = config["broadcast"]["messages_count"]
        self.trigger: str = config["broadcast"]["trigger"]
        self.delay: List[int] = config["broadcast"]["delay"]

    def save(
        self,
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
            )
        )

        with open("config.toml", "w") as file:
            toml.dump(config, file)

    def initial_setup(self):
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
        api_id, api_hash = self.setup_sessions()

        console.print(
            "[bold blue]Broadcast[/]",
            justify="center"
        )

        print()
        messages, delay, trigger = self.setup_broadcast()

        self.save(
            api_id,
            api_hash,
            messages,
            delay,
            0,
            trigger
        )

    def setup_sessions(self) -> Tuple[int, str]:
        api_id = console.input("[bold white]Enter API ID: [/]")
        api_hash = console.input("[bold white]Enter API hash: [/]")

        return int(api_id), api_hash

    def setup_broadcast(self) -> Tuple[List[str], List[int], str]:
        console.print("[bold white]Enter messages[/]")

        messages = []

        while message := console.input("[bold white]>> [/]"):
            messages.append(message)

        print()

        delay = console.input("[bold white]Sending delay (e.g. 1-3): [/]")
        delay = delay.split("-")
        delay = [int(x) for x in delay]

        trigger = console.input("[bold white]Enter the trigger text after which accounts start the broadcast: [/]")

        return messages, delay, trigger


