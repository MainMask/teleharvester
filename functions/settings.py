from modules.console import console
from rich.prompt import Prompt

from functions.base import TelethonFunction
from modules.settings import seconds_text


class SettingsFunc(TelethonFunction):
    """Settings"""

    # ⚙️ Настройки, as the bot's (bot/routers/settings.py): written to config.toml and to this live
    # Settings, so the other functions' delay defaults change at once. The auto-reply runs in the bot only.

    def execute(self):
        autoreply = "вкл" if self.settings.autoreply_enabled and self.settings.autoreply_text else "выкл"
        console.print(f"  [1] ⏱ Задержка: {seconds_text(self.settings.delay)}\n"
                      f"  [2] ⏳ Пауза профиля: {seconds_text(self.settings.profile_pause)}\n"
                      f"  Автоответ: {autoreply} — настраивается в боте  (пусто — выход)",
                      markup=False, highlight=False)
        action = Prompt.ask("[bold red]что изменить[/]", default="")
        if action == "1":
            delay = self.ask_seconds(
                "[bold red]задержка между действиями воркера, с: 5-10 (случайная в диапазоне) или 7[/]")
            self._save(self.settings.set_delay, delay, "Задержка")
        elif action == "2":
            pause = self.ask_seconds("[bold red]пауза между аккаунтами при смене профиля, с: 60-180 или 120[/]")
            self._save(self.settings.set_profile_pause, pause, "Пауза профиля")

    def _save(self, setter, seconds: list, title: str):
        try:
            setter(seconds)
        except (OSError, ValueError) as err:  # ValueError: a config.toml broken by hand meanwhile
            console.print(f"[bold red]Не удалось сохранить config.toml:[/] {self.safe(err)}")
            return
        console.print(f"[bold green]{title}: {seconds_text(seconds)}. Сохранено в config.toml, действует сразу.[/]")
