from modules.console import console
from rich.prompt import Prompt

from functions.base import TelethonFunction
from modules.registry import AUTOREPLY, title
from modules.settings import count_text, seconds_text


# 📏 Дневные лимиты, as the bot's: [limits] in config.toml, under their buttons' titles
DAILY_CAPS = {"per_account_daily": title("cap_pm"), "invite_per_account_daily": title("cap_invite"),
              "contacts_per_account_daily": title("cap_contacts")}


class SettingsFunc(TelethonFunction):
    """Settings"""

    # ⚙️ Настройки, as the bot's (bot/routers/settings.py): written to config.toml and to this live
    # Settings, so the other functions' delay defaults change at once. The auto-reply runs in the bot only;
    # the chat campaign's trigger and count are asked by 🔔 Чат — по слову itself (functions/broadcast_chat.py).

    def execute(self):
        autoreply = "включён" if self.settings.autoreply_enabled and self.settings.autoreply_text else "выключен"
        console.print(f"  [1] {title('delay')}: {seconds_text(self.settings.delay)}\n"
                      f"  [2] {title('ppause')}: {seconds_text(self.settings.profile_pause)}\n"
                      + "".join(f"  [{number}] {name} в сутки на аккаунт: {count_text(getattr(self.settings, key))}\n"
                                for number, (key, name) in enumerate(DAILY_CAPS.items(), 3)) +
                      f"  {AUTOREPLY}: {autoreply} — настраивается в боте  (пусто — выход)",
                      markup=False, highlight=False)
        action = Prompt.ask("[bold red]что изменить[/]", default="")
        if action == "1":
            delay = self.ask_seconds(
                "[bold red]задержка между действиями воркера, с: 5-10 (случайная в диапазоне) или 7[/]")
            self._save(self.settings.set_delay, delay, title("delay"), seconds_text(delay))
        elif action == "2":
            pause = self.ask_seconds("[bold red]пауза между аккаунтами при смене профиля, с: 60-180 или 120[/]")
            self._save(self.settings.set_profile_pause, pause, title("ppause"), seconds_text(pause))
        elif action in ("3", "4", "5"):
            key, name = list(DAILY_CAPS.items())[int(action) - 3]
            count = self.ask_int(f"[bold red]{name} в сутки на аккаунт (0 = без лимита)[/]", min_value=0)
            self._save(lambda value: self.settings.set_daily_cap(key, value), count, name, count_text(count))

    def _save(self, setter, value, name: str, shown: str):
        try:
            setter(value)
        except (OSError, ValueError) as err:  # ValueError: a config.toml broken by hand meanwhile
            console.print(f"[bold red]Не удалось сохранить config.toml:[/] {self.safe(err)}")
            return
        console.print(f"{name}: {shown}. Сохранено в config.toml, действует сразу.", style="bold green",
                      markup=False)
