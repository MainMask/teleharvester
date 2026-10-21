from modules.console import console
from rich.prompt import Prompt

from functions.base import TelethonFunction
from modules.settings import count_text, seconds_text


# 📏 Дневные лимиты, as the bot's: [limits] in config.toml
DAILY_CAPS = {"per_account_daily": "📨 ЛС", "invite_per_account_daily": "🤝 Инвайты",
              "contacts_per_account_daily": "📇 Контакты"}


class SettingsFunc(TelethonFunction):
    """Settings"""

    # ⚙️ Настройки, as the bot's (bot/routers/settings.py): written to config.toml and to this live
    # Settings, so the other functions' delay defaults change at once. The auto-reply runs in the bot only.

    def execute(self):
        autoreply = "вкл" if self.settings.autoreply_enabled and self.settings.autoreply_text else "выкл"
        trigger = f"«{self.settings.trigger}»" if self.settings.trigger else "не задан"
        console.print(f"  [1] ⏱ Задержка: {seconds_text(self.settings.delay)}\n"
                      f"  [2] ⏳ Пауза профиля: {seconds_text(self.settings.profile_pause)}\n"
                      f"  [3] 🔔 Триггер: {trigger}\n"
                      f"  [4] 🔢 Сообщений на воркера: {count_text(self.settings.messages_count)}\n"
                      + "".join(f"  [{number}] {title} в сутки на аккаунт: {count_text(getattr(self.settings, key))}\n"
                                for number, (key, title) in enumerate(DAILY_CAPS.items(), 5)) +
                      f"  Автоответ: {autoreply} — настраивается в боте  (пусто — выход)",
                      markup=False, highlight=False)
        action = Prompt.ask("[bold red]что изменить[/]", default="")
        if action == "1":
            delay = self.ask_seconds(
                "[bold red]задержка между действиями воркера, с: 5-10 (случайная в диапазоне) или 7[/]")
            self._save(self.settings.set_delay, delay, "Задержка", seconds_text(delay))
        elif action == "2":
            pause = self.ask_seconds("[bold red]пауза между аккаунтами при смене профиля, с: 60-180 или 120[/]")
            self._save(self.settings.set_profile_pause, pause, "Пауза профиля", seconds_text(pause))
        elif action == "3":
            trigger = ""
            while not trigger:
                trigger = Prompt.ask("[bold red]кодовое слово, после которого воркеры пишут в чат[/]").strip()
            self._save(self.settings.set_trigger, trigger, "Триггер", f"«{trigger}»")
        elif action == "4":
            count = self.ask_int("[bold red]сколько сообщений отправит каждый воркер (0 = без лимита)[/]",
                                 min_value=0)
            self._save(self.settings.set_messages_count, count, "Сообщений на воркера", count_text(count))
        elif action in ("5", "6", "7"):
            key, title = list(DAILY_CAPS.items())[int(action) - 5]
            count = self.ask_int(f"[bold red]{title} в сутки на аккаунт (0 = без лимита)[/]", min_value=0)
            self._save(lambda value: self.settings.set_daily_cap(key, value), count, title, count_text(count))

    def _save(self, setter, value, title: str, shown: str):
        try:
            setter(value)
        except (OSError, ValueError) as err:  # ValueError: a config.toml broken by hand meanwhile
            console.print(f"[bold red]Не удалось сохранить config.toml:[/] {self.safe(err)}")
            return
        console.print(f"{title}: {shown}. Сохранено в config.toml, действует сразу.", style="bold green",
                      markup=False)
