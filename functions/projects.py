from modules.console import console
from rich.prompt import Confirm, Prompt

# the modules, not their *Func classes: an imported *Func would be a second menu entry
from functions import add_contacts, pmmailing, scraper, scraper_analysis
from functions.base import TelethonFunction
from bot.services.registry import PROJECT_CARD, PROJECT_MANAGE, card_rows
from modules import projects, scraped_files
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
        # a card's mailing or contacts is a coroutine: run_instance awaits it on the sessions' loop
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
        console.print(projects.card(project), markup=False, highlight=False)
        # a top-up while a scrape is unfinished would drop its checkpoint: ▶️ continues that first
        plan = projects.topup(project) if projects.unfinished(project.name) is None else None
        # the bases only ever added to: a group's members, the uploaded list
        clearable = [what for what, path in projects.CLEARABLE.items() if path(project.name).exists()]
        # the bot's card and its ⚙️ in one list: every function, data or not (one with nothing to work on
        # yet says what to collect first: projects.missing), then the project itself; 🔄 and 🧹 when there
        # is something to do. Numbered by their place in the registry, so a hidden one shifts none
        hidden = ([] if plan else ["topup"]) + ([] if clearable else ["clear"])
        actions = [button.action for rows in (PROJECT_CARD, PROJECT_MANAGE) for row in rows for button in row
                   if button.action != "manage"]
        number = {action: index for index, action in enumerate(actions, 1)}
        card, manage = ("  ".join(f"[{number[action]}] {title}"
                                  for row in card_rows(rows, hidden, bool(project.channels))
                                  for title, action in row if action in number)
                        for rows in (PROJECT_CARD, PROJECT_MANAGE))
        console.print(f"  {card}\n  ⚙️ Управление проектом:  {manage}  (пусто — выход)", markup=False, highlight=False)
        shown = {str(number[action]): action for action in actions if action not in hidden}
        action = shown.get(Prompt.ask("[bold red]действие[/]", default=""))
        checked = ("scrape", "contacts", "mail", "verify", "analysis")
        if action in checked and (why := projects.missing(project, action)):
            console.print(why, style="bold yellow", markup=False)
            return
        # each runs the function's own flow on the project's files only
        if action == "scrape":
            scraper.ScrapeFunc(self.storage, self.settings).scrape(project.name, project.channels)
        elif action == "topup":
            scraper.ScrapeFunc(self.storage, self.settings).topup(project.name, plan)
        elif action == "members":  # the base is named after the project: one of its bases
            scraper.MembersFunc(self.storage, self.settings).execute(project=project.name)
        elif action == "contacts":  # the contacts need a base's IDs
            bases = scraped_files.project_files(project.name, kinds=("participants",))
            return add_contacts.AddContactsFunc(self.storage, self.settings).execute(bases=bases)
        elif action == "mail":  # the mailing takes the uploaded list too
            recipients = scraped_files.project_files(project.name, kinds=("participants", "list"))
            return pmmailing.PmMailingFunc(self.storage, self.settings).execute(bases=recipients)
        elif action == "upload":
            self._upload(project)
        elif action == "clear":
            self._clear(project, clearable)
        elif action == "verify":
            posts = scraped_files.project_files(project.name, kinds=("posts",))
            scraper.VerifyFunc(self.storage, self.settings).execute(posts=posts, project=project.name)
        elif action == "analysis":
            scraper_analysis.ScraperAnalysisFunc(self.storage, self.settings).execute(project=project.name)
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
            f"[bold red]удалить проект '{self.safe(project.name)}'? файлы скрапа останутся[/]", default=False,
        ):
            try:
                projects.remove(project.name)
            except (ValueError, OSError) as err:
                console.print(f"[bold red]Не удалено:[/] {self.safe(err)}")
                return
            console.print("[bold green]Проект удалён, файлы скрапа на месте.[/]")
