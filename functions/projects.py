from modules.console import console
from rich.prompt import Confirm, Prompt

# the modules, not their *Func classes: an imported *Func would be a second menu entry
from functions import add_contacts, pmmailing, scraper, scraper_analysis
from functions.base import TelethonFunction
from modules import projects, scraped_files
from scraper.scrape import parse_channels


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
        channels = parse_channels(Prompt.ask("[bold red]каналы через запятую[/]"))
        try:
            project = projects.add(name, channels)
        except (ValueError, OSError) as err:
            console.print(f"[bold red]Проект не создан:[/] {self.safe(err)}")
            return
        return self._card(project)

    def _import(self):
        """A project from a finished scrape: its name and the channels kept in its posts file."""
        found = projects.importable()
        if not found:
            console.print("[bold yellow]Не из чего сделать проект: у готовых скрапов уже есть проекты, "
                          "или в их файлах не записаны каналы.[/]")
            return
        for index, (name, channels) in enumerate(found, 1):
            console.print(f"  [{index}] {name} · каналов: {len(channels)}", markup=False, highlight=False)
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
        # a top-up while a scrape is unfinished would drop its checkpoint: [1] continues that first
        plan = projects.topup(project) if projects.unfinished(project.name) is None else None
        # the work on the project's data, as on the bot's card: each runs the section's own flow on its files only
        bases = scraped_files.project_files(project.name, kinds=("participants",))
        posts = scraped_files.project_files(project.name, kinds=("posts",))
        console.print("  [1] ▶️ Скрап проекта  [2] ✏️ Каналы  [3] 🗑 Удалить"
                      f"{'  [4] 🔄 Дособрать' if plan else ''}  [5] 🏷 Переименовать"
                      f"{'  [6] 📨 Рассылка по базе  [7] 📇 В контакты' if bases else ''}"
                      f"{'  [8] ✅ Верификация  [9] 📈 Анализ' if posts else ''}  (пусто — выход)",
                      markup=False, highlight=False)
        action = Prompt.ask("[bold red]действие[/]", default="")
        if action == "1":
            scraper.ScrapeFunc(self.storage, self.settings).scrape(project.name, project.channels)
        elif action == "4" and plan:
            scraper.ScrapeFunc(self.storage, self.settings).topup(project.name, plan)
        elif action == "6" and bases:
            return pmmailing.PmMailingFunc(self.storage, self.settings).execute(bases=bases)
        elif action == "7" and bases:
            return add_contacts.AddContactsFunc(self.storage, self.settings).execute(bases=bases)
        elif action == "8" and posts:
            scraper.VerifyFunc(self.storage, self.settings).execute(posts=posts)
        elif action == "9" and posts:
            scraper_analysis.ScraperAnalysisFunc(self.storage, self.settings).execute(project=project.name)
        elif action == "5":
            new = Prompt.ask(f"[bold red]новое имя проекта '{self.safe(project.name)}' "
                             "(файлы проекта переименуются вместе с ним)[/]")
            try:
                renamed, moved = projects.rename(project.name, new)
            except (ValueError, OSError) as err:
                console.print(f"[bold red]Не переименовано:[/] {self.safe(err)}")
                return
            console.print(f"[bold green]Проект теперь '{self.safe(renamed.name)}'. "
                          f"Переименовано файлов и папок: {moved}.[/]")
        elif action == "2":
            channels = parse_channels(Prompt.ask("[bold red]новый список каналов через запятую (заменит текущий)[/]"))
            try:
                projects.set_channels(project.name, channels)
            except (ValueError, OSError) as err:
                console.print(f"[bold red]Не сохранено:[/] {self.safe(err)}")
                return
            console.print("[bold green]Каналы проекта сохранены.[/]")
        elif action == "3" and Confirm.ask(
            f"[bold red]удалить проект '{self.safe(project.name)}'? файлы скрапа останутся[/]", default=False,
        ):
            try:
                projects.remove(project.name)
            except (ValueError, OSError) as err:
                console.print(f"[bold red]Не удалено:[/] {self.safe(err)}")
                return
            console.print("[bold green]Проект удалён, файлы скрапа на месте.[/]")
