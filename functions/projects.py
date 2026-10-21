from modules.console import console
from rich.prompt import Confirm, Prompt

# the modules, not their *Func classes: an imported *Func would be a second menu entry
from functions import add_contacts, pmmailing, scraper, scraper_analysis
from functions.base import TelethonFunction
from modules.cli_menu import menu_entries, print_menu
from modules.registry import (ADD_CHANNELS, CARD, PROJECT_FILES, RISKY, SCREENS, by_category, children, risk_marker,
                              trail)
from modules import projects, scraped_files
from modules.storages.functions_storage import FunctionsStorage
from scraper.scrape import bad_channels, bad_channels_text, parse_channels


class ProjectsFunc(TelethonFunction):
    """Projects"""

    def execute(self):
        try:
            items = projects.load()
        except ValueError as err:
            console.print(f"[bold red]{self.safe(err)}[/]")
            return

        for index, project in enumerate(items, 1):
            console.print(f"  [{index}] {projects.label(project)}", markup=False, highlight=False)
        console.print(f"  [{len(items) + 1}] ➕ Новый проект", markup=False, highlight=False)
        console.print(f"  [{len(items) + 2}] 📥 Из готового скрапа", markup=False, highlight=False)
        choice = self.ask_int("[bold red]проект[/]", min_value=1)
        if choice == len(items) + 1:
            return self._new(items)
        elif choice == len(items) + 2:
            return self._import()
        elif choice <= len(items):
            return self._card(items[choice - 1])

    def _new(self, items):
        name = Prompt.ask("[bold red]имя проекта (оно же имя файлов скрапа; готовые файлы с ним войдут в проект)[/]")
        if problem := projects.name_problem(name.strip(), items):
            console.print(f"[bold red]Не подходит: {self.safe(problem)}.[/]")
            return
        channels = self._ask_channels("[bold red]каналы через запятую (Enter — без каналов: база из участников "
                                      "групп или списка)[/]")
        try:
            project = projects.add(name, channels)
        except (ValueError, OSError) as err:
            console.print(f"[bold red]Проект не создан:[/] {self.safe(err)}")
            return
        return self._card(project)

    def _ask_channels(self, prompt: str) -> list[str]:
        """The typed channels ([] for Enter), asked again while one of them can't be a channel."""
        while True:
            channels = parse_channels(Prompt.ask(prompt, default=""))
            if not (bad := bad_channels(channels)):
                return channels
            console.print(f"{bad_channels_text(bad)} Введите заново.", style="bold red", markup=False)

    def _upload(self, project):
        """Add a .txt's lines to the project's list (modules/projects.add_to_list), as the bot's 📥."""
        path = Prompt.ask("[bold red]путь к .txt (по одному @username, ссылке t.me или номеру на строку)[/]")
        try:
            with open(path, encoding="utf-8", errors="replace") as file:
                added, total, skipped = projects.add_to_list(project.name, file.read().splitlines())
        except OSError as err:
            console.print(f"[bold red]Список не добавлен:[/] {self.safe(err)}")
            return
        note = f" Пропущено строк: {skipped} — не @username и не номер." if skipped else ""
        console.print(f"В список проекта добавлено: {added}, всего в нём: {total}.{note}",
                      style="bold green", markup=False)

    def _clear(self, project, clearable):
        """Move the project's group members' base or its list to <name>_previous/, as the bot's 🧹."""
        labels = {"members": "участники групп", "list": "список"}
        for index, what in enumerate(clearable, 1):
            console.print(f"  [{index}] 🧹 {labels[what]}", markup=False, highlight=False)
        choice = self.ask_int("[bold red]что очистить[/]", min_value=1)
        if choice > len(clearable):
            return
        what = clearable[choice - 1]
        if not Confirm.ask(f"[bold red]очистить {labels[what]} проекта '{self.safe(project.name)}'? файл уйдёт в "
                           f"{scraped_files.BASES_DIR}/{self.safe(project.name)}_previous/ (до следующего дособора)[/]",
                           default=False):
            return
        try:
            moved = projects.clear(project.name, what)
        except (ValueError, OSError) as err:
            console.print(f"[bold red]Не очищено:[/] {self.safe(err)}")
            return
        console.print(f"Очищено: {labels[what]}. Файл — в {moved}.", style="bold green", markup=False)

    def _import(self):
        """A project from finished files: their name and the channels kept in its posts file, if any."""
        found = projects.importable()
        if not found:
            console.print("[bold yellow]Не из чего сделать проект: у всех готовых файлов уже есть проекты.[/]")
            return
        for index, (name, channels) in enumerate(found, 1):
            console.print(f"  [{index}] {projects.label(projects.Project(name, channels))}", markup=False,
                          highlight=False)
        choice = self.ask_int("[bold red]скрап[/]", min_value=1)
        if choice > len(found):
            return
        try:
            project = projects.add(*found[choice - 1])
        except (ValueError, OSError) as err:
            console.print(f"[bold red]Проект не создан:[/] {self.safe(err)}")
            return
        return self._card(project)

    def _card(self, project):
        """The project's card, on its own workers — each connected for the work it is in, as in the bot —:
        one action, then back to the menu."""
        storage = projects.storage(project, self.settings.api_id, self.settings.api_hash)
        functions = FunctionsStorage("functions", storage, self.settings)
        console.print(projects.card(project, len(storage)), markup=False, highlight=False)
        # a top-up while a scrape is unfinished would drop its checkpoint: ▶️ continues that first
        plan = projects.topup(project) if projects.unfinished(project.name) is None else None
        # the bases only ever added to: a group's members, the uploaded list
        clearable = [what for what, path in projects.CLEARABLE.items() if path(project.name).exists()]
        # the bot's card's screens in its order: one on the project's files a line, each of its buttons, data or
        # not (one with nothing to work on yet says what to collect first: projects.missing), 🔄 and 🧹 when there
        # is something to do; the ones on its workers (🤖 Аккаунты, 💬 Активность) on a line of their own.
        # Numbered by their place in the registry, so a hidden one shifts none
        hidden = ([] if plan else ["topup"]) + ([] if clearable else ["clearbase"])
        titles = {"channels": ADD_CHANNELS} if not project.channels else {}
        items = {title: self._items(title) if title in PROJECT_FILES else [(f"{title} ›", title)]
                 for title in children(CARD)}
        keys = [key for title in children(CARD) for _, key in items[title]]
        number = {key: index for index, key in enumerate(keys, 1)}
        lines = []
        for title in children(CARD):
            shown = "  ".join(f"[{number[key]}] {titles.get(key, label)}" for label, key in items[title]
                              if key not in hidden)
            if title in PROJECT_FILES:
                lines.append(f"{title}{self._risky(title)}:  {shown}")
            elif lines and lines[-1].startswith("["):  # the screens on its workers: one line
                lines[-1] += f"  {shown}"
            else:
                lines.append(shown)
        lines.append("(пусто — выход)")
        console.print("\n".join(f"  {line}" for line in lines), markup=False, highlight=False)
        shown = {str(number[key]): key for key in keys if key not in hidden}
        action = shown.get(Prompt.ask("[bold red]действие[/]", default=""))
        if action in SCREENS:  # 🤖 Аккаунты, 💬 Активность
            return self._screen(action, functions)
        if action in projects.MISSING_CHECKED and (why := projects.missing(project, action)):
            console.print(why, style="bold yellow", markup=False)
            return
        # each runs the function's own flow on the project's files only, on its workers
        if action == "scrape":
            scraper.ScrapeFunc(storage, self.settings).scrape(project.name, project.channels)
        elif action == "topup":
            scraper.ScrapeFunc(storage, self.settings).topup(project.name, plan)
        elif action == "members":  # the base is named after the project: one of its bases
            scraper.MembersFunc(storage, self.settings).execute(project=project.name)
        elif action == "addcontacts":  # the contacts need a base's IDs
            bases = scraped_files.project_files(project.name, kinds=("participants",))
            functions.run(add_contacts.AddContactsFunc(storage, self.settings).execute(bases=bases))
        elif action == "pmmailing":  # the mailing takes the uploaded list too
            recipients = scraped_files.project_files(project.name, kinds=("participants", "list"))
            functions.run(pmmailing.PmMailingFunc(storage, self.settings).execute(bases=recipients))
        elif action == "upload":
            self._upload(project)
        elif action == "clearbase":
            self._clear(project, clearable)
        elif action == "verify":
            posts = scraped_files.project_files(project.name, kinds=("posts",))
            scraper.VerifyFunc(storage, self.settings).execute(posts=posts, project=project.name)
        elif action == "analysis":
            scraper_analysis.ScraperAnalysisFunc(storage, self.settings).execute(project=project.name)
        elif action == "rename":
            new = Prompt.ask(f"[bold red]новое имя проекта '{self.safe(project.name)}' "
                             "(файлы проекта переименуются вместе с ним)[/]")
            try:
                renamed, moved = projects.rename(project.name, new)
            except (ValueError, OSError) as err:
                console.print(f"[bold red]Не переименовано:[/] {self.safe(err)}")
                return
            console.print(f"[bold green]Проект теперь '{self.safe(renamed.name)}'. "
                          f"Переименовано файлов и папок: {moved}.[/]")
        elif action == "channels":
            empty = "убрать все каналы" if project.channels else "оставить без каналов"
            channels = self._ask_channels(f"[bold red]новый список каналов через запятую (заменит текущий; "
                                          f"Enter — {empty})[/]")
            try:
                projects.set_channels(project.name, channels)
            except (ValueError, OSError) as err:
                console.print(f"[bold red]Не сохранено:[/] {self.safe(err)}")
                return
            console.print("[bold green]Каналы проекта сохранены.[/]")
        elif action == "delete" and Confirm.ask(
            f"[bold red]удалить проект '{self.safe(project.name)}'? файлы скрапа останутся; проект с аккаунтами не "
            "удаляется — сначала перенесите их[/]", default=False,
        ):
            try:
                projects.remove(project.name)
            except (ValueError, OSError) as err:
                console.print(f"[bold red]Не удалено:[/] {self.safe(err)}")
                return
            console.print("[bold green]Проект удалён, файлы скрапа на месте.[/]")

    @staticmethod
    def _items(title) -> list[tuple[str, str]]:
        """A card's screen's buttons, (title, key), as the bot's: its actions, then its functions (⚠️ as there)."""
        functions = by_category(title)
        return [*((button.title, button.action) for button in SCREENS[title].buttons),
                *((f"{risk_marker(function, functions)}{function.title}", function.key) for function in functions)]

    @staticmethod
    def _risky(title) -> str:
        """⚠️ on a screen's title when every function there is risky, as in the bot."""
        functions = by_category(title)
        return " ⚠️" if functions and all(function.risk == RISKY for function in functions) else ""

    @staticmethod
    def _screen(title, functions):
        """A project's screen on its workers (🤖 Аккаунты with 👤 Профиль, 🔐 Безопасность; 💬 Активность): its
        items, one run on the project's workers."""
        entries = [entry for entry in menu_entries(functions.functions, project=True) if title in trail(entry[0])]
        print_menu(entries)
        raw = Prompt.ask("[bold red]пункт (пусто — выход)[/]", default="")
        if raw.isdigit() and 1 <= int(raw) <= len(entries):
            functions.run_instance(entries[int(raw) - 1][1])
