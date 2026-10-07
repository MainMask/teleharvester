import json
import os
import re
import shutil
import sys
import toml
from dotenv import set_key
from modules.console import console
from typing import List, Tuple

from modules.config import load_toml


# one-line TOML value: a flat list, a basic string (escapes kept), a bool or a number
_VALUE = r'\[[^\]]*\]|"(?:[^"\\]|\\.)*"|true|false|-?\d+'


def _toml_literal(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return f"[{', '.join(str(part) for part in value)}]"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)  # a JSON string is a TOML basic string
    return str(value)


def _replace_value_line(text: str, section_name: str, key: str, literal: str) -> str | None:
    """text with `section_name`'s one-line `key = ...` set to literal (a trailing comment
    kept); a missing key is added under the section's header and a missing section at the
    end. None if the key's value isn't on one line (e.g. a multi-line list)."""
    lines = text.splitlines(keepends=True)
    section = header_index = None
    for index, line in enumerate(lines):
        header = re.match(r"\s*\[([^\]]+)\]\s*(#.*)?$", line)
        if header:
            section = header.group(1).strip()
            if section == section_name:
                header_index = index
            continue
        if section != section_name or not re.match(rf"\s*{re.escape(key)}\s*=", line):
            continue
        match = re.match(rf"(\s*{re.escape(key)}\s*=\s*)(?:{_VALUE})(.*)$", line, re.DOTALL)
        if not match:
            return None
        lines[index] = f"{match.group(1)}{literal}{match.group(2)}"
        return "".join(lines)

    if header_index is None:
        separator = "" if not text or text.endswith("\n") else "\n"
        return f"{text}{separator}\n[{section_name}]\n{key} = {literal}\n"
    if not lines[header_index].endswith("\n"):
        lines[header_index] += "\n"
    lines.insert(header_index + 1, f"{key} = {literal}\n")
    return "".join(lines)


# [limits] when config.toml leaves a key out; also what a new config.toml gets (see save)
LIMIT_DEFAULTS = dict(
    per_account_daily=30,
    account_pause=[30, 60],
    profile_pause=[60, 180],
    invite_per_account_daily=0,
    contacts_per_account_daily=0,
)

MIN_AUTOREPLY_INTERVAL = 60  # seconds: each check connects every worker


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_pause(value) -> bool:
    """[sec] or [min, max] of non-negative whole seconds (as the bot's delay input takes)."""
    return isinstance(value, list) and 1 <= len(value) <= 2 and all(_is_int(v) and v >= 0 for v in value)


class Settings:
    def __init__(self):
        config = load_toml("config.toml")

        if not config:
            # Not interactive here: the terminal menu runs ensure_config() first, so
            # only non-CLI entry points (the bot) reach this, and they get a clear
            # message instead of a setup wizard.
            console.print("[bold red]config.toml не найден. Запустите `python main.py` один раз, чтобы создать его.[/]")
            raise SystemExit(1)

        if "sessions" in config:
            console.print(
                "[bold red]Ключи API переехали в .env: перенесите \\[sessions] api_id/api_hash в "
                "TG_API_ID/TG_API_HASH и удалите секцию \\[sessions] из config.toml.[/]"
            )
            raise SystemExit(1)

        api_id = os.environ.get("TG_API_ID", "")
        self.api_hash: str = os.environ.get("TG_API_HASH", "")

        if not api_id.isdigit() or not self.api_hash:
            console.print("[bold red]Укажите TG_API_ID и TG_API_HASH в .env (см. .env.example).[/]")
            raise SystemExit(1)

        self.api_id: int = int(api_id)

        try:
            self.messages: List[str] = config["broadcast"]["messages"]
            self.messages_count: int = config["broadcast"]["messages_count"]
            self.trigger: str = config["broadcast"]["trigger"]
            self.delay: List[int] = config["broadcast"]["delay"]
        except KeyError as err:
            console.print(
                f"[bold red]В config.toml нет {err}. "
                "Удалите его и запустите `python main.py`, чтобы создать заново.[/]"
            )
            raise SystemExit(1)

        limits = {**LIMIT_DEFAULTS, **config.get("limits", {})}
        self.per_account_daily: int = limits["per_account_daily"]
        self.account_pause: List[int] = limits["account_pause"]
        # pause between accounts for profile-wide changes (name/bio/username/2FA/photo/…):
        # bigger than account_pause so identical edits don't land on every account at once
        self.profile_pause: List[int] = limits["profile_pause"]
        # per-account daily caps (0 = unlimited): invites / contact adds a worker makes per day
        self.invite_per_account_daily: int = limits["invite_per_account_daily"]
        self.contacts_per_account_daily: int = limits["contacts_per_account_daily"]

        # bot only: one reply to people who answer a PM mailing (bot/services/autoreply.py);
        # off when disabled or the text is empty (or there is no [autoreply] section)
        autoreply = config.get("autoreply", {})
        self.autoreply_enabled: bool = autoreply.get("enabled", True)
        self.autoreply_text: str = autoreply.get("text", "")
        self.autoreply_interval: int = autoreply.get("interval", 900)

        # a hand-edited value would only fail mid-job (an empty pause) or hammer the workers
        # (an auto-reply check with no pause): stop at start instead, like a missing key
        for name, pause in (("[broadcast] delay", self.delay), ("[limits] account_pause", self.account_pause),
                            ("[limits] profile_pause", self.profile_pause)):
            if not _is_pause(pause):
                console.print(f"[bold red]config.toml {name}: нужно [сек] или [мин, макс] "
                              f"(целые секунды, не меньше 0), а не {pause!r}.[/]")
                raise SystemExit(1)
        for name, cap in (("[broadcast] messages_count", self.messages_count),
                          ("[limits] per_account_daily", self.per_account_daily),
                          ("[limits] invite_per_account_daily", self.invite_per_account_daily),
                          ("[limits] contacts_per_account_daily", self.contacts_per_account_daily)):
            if not _is_int(cap) or cap < 0:
                console.print(f"[bold red]config.toml {name}: нужно целое число, 0 — без лимита, "
                              f"а не {cap!r}.[/]")
                raise SystemExit(1)
        if not isinstance(self.trigger, str):
            console.print("[bold red]config.toml [broadcast] trigger должен быть строкой в кавычках.[/]")
            raise SystemExit(1)
        # a quoted enabled = "false" is a non-empty string, i.e. true: the auto-reply would run
        if not isinstance(self.autoreply_enabled, bool) or not isinstance(self.autoreply_text, str):
            console.print("[bold red]config.toml [autoreply] enabled должен быть true/false (без кавычек), "
                          "а text — строкой в кавычках.[/]")
            raise SystemExit(1)
        if not _is_int(self.autoreply_interval) or self.autoreply_interval < MIN_AUTOREPLY_INTERVAL:
            console.print(f"[bold red]config.toml [autoreply] interval должен быть не меньше "
                          f"{MIN_AUTOREPLY_INTERVAL} секунд, а не {self.autoreply_interval!r}.[/]")
            raise SystemExit(1)

    @staticmethod
    def _write_setting(path: str, section: str, key: str, value):
        """Set section.key to value in config.toml, keeping every other setting (save()
        would reset [limits] to its defaults) and the file's comments.

        Only the single `key = ...` line is rewritten (or added); a layout that line can't
        be found in is rewritten whole."""
        with open(path, encoding="utf-8") as file:
            text = file.read()

        updated = _replace_value_line(text, section, key, _toml_literal(value))
        try:
            written = updated is not None and toml.loads(updated).get(section, {}).get(key) == value
        except toml.TomlDecodeError:
            written = False
        if not written:
            config = toml.loads(text)
            config.setdefault(section, {})[key] = value
            updated = toml.dumps(config)

        # atomic: the file also holds the bot's admin whitelist
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as file:
            file.write(updated)
            file.flush()
            os.fsync(file.fileno())  # on disk before the rename: a power cut must not leave it empty
        shutil.copymode(path, tmp_path)  # the rename must not widen a locked-down file's mode
        os.replace(tmp_path, path)

    def set_delay(self, delay: List[int], path: str = "config.toml"):
        """Change the delay between actions in config.toml and in this live object."""
        self._write_setting(path, "broadcast", "delay", delay)
        self.delay = delay

    def set_trigger(self, trigger: str, path: str = "config.toml"):
        """Change the chat campaign's trigger text, in config.toml and live."""
        self._write_setting(path, "broadcast", "trigger", trigger)
        self.trigger = trigger

    def set_messages_count(self, count: int, path: str = "config.toml"):
        """Change how many messages each worker sends per chat/post (0 = unlimited), in config.toml and live."""
        self._write_setting(path, "broadcast", "messages_count", count)
        self.messages_count = count

    def set_profile_pause(self, pause: List[int], path: str = "config.toml"):
        """Change the pause between accounts for profile changes, in config.toml and live."""
        self._write_setting(path, "limits", "profile_pause", pause)
        self.profile_pause = pause

    def set_autoreply_enabled(self, enabled: bool, path: str = "config.toml"):
        """Turn the bot's auto-reply on/off, in config.toml and live (the loop reads it)."""
        self._write_setting(path, "autoreply", "enabled", enabled)
        self.autoreply_enabled = enabled

    def set_autoreply_text(self, text: str, path: str = "config.toml"):
        """Change the auto-reply text, in config.toml and live."""
        self._write_setting(path, "autoreply", "text", text)
        self.autoreply_text = text

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
            limits=LIMIT_DEFAULTS
        )

        with open("config.toml", "w", encoding="utf-8") as file:
            toml.dump(config, file)

    @staticmethod
    def initial_setup():
        console.print(
            "[bold yellow]Первичная настройка[/]",
            justify="center"
        )

        print()

        if not os.environ.get("TG_API_ID") or not os.environ.get("TG_API_HASH"):
            console.print(
                "[bold blue]Сессии[/]",
                justify="center"
            )

            print()
            api_id, api_hash = Settings.setup_sessions()

            # secrets go to .env, not config.toml
            set_key(".env", "TG_API_ID", str(api_id))
            set_key(".env", "TG_API_HASH", api_hash)

        console.print(
            "[bold blue]Рассылка[/]",
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
        api_id = console.input("[bold white]Введите API ID: [/]")

        while not api_id.isdigit():
            api_id = console.input("[bold white]API ID должен быть числом: [/]")

        api_hash = console.input("[bold white]Введите API hash: [/]")

        return int(api_id), api_hash

    @staticmethod
    def setup_broadcast() -> Tuple[List[str], List[int], str]:
        console.print("[bold white]Введите сообщения (пустая строка — конец)[/]")

        messages = []

        while (message := console.input("[bold white]>> [/]")) or not messages:
            if message:  # at least one message: the CLI broadcast picks from this list
                messages.append(message)

        print()

        while True:
            parts = console.input("[bold white]Задержка между отправками (например 1-3): [/]").split("-")
            # [sec] or [min, max] only: Settings() rejects any other delay at the next start
            if 1 <= len(parts) <= 2 and all(part.strip().isdigit() for part in parts):
                delay = [int(part) for part in parts]
                break

        trigger = console.input("[bold white]Триггер — текст, после которого аккаунты начнут рассылку: [/]")

        return messages, delay, trigger


