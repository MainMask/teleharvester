from pathlib import Path

from modules.console import console
from rich.prompt import Prompt

from functions.base import TelethonFunction

from modules import analysis_tools, scraped_files
from modules.scraped_files import output_path
from scraper import analysis
from scraper.datafiles import read_table, save_table


# project: the project's name, run from its card (functions/projects.py): its files are offered only

def _posts_file(project) -> str:
    """A scraped posts file: a number from assets/databases or a typed path."""
    files = scraped_files.project_files(project, kinds=("posts",))
    return TelethonFunction.ask_file("[bold red]файл постов[/]", files)


def _combine(project):
    # blank: all the project's posts files, as the bot's «🗂 Все файлы постов»
    inp = Prompt.ask("[bold red]что объединить (файл / папка / шаблон *.parquet; пусто — все файлы постов "
                     "проекта)[/]", default="")
    inp = inp or [path for path, _ in scraped_files.scrape_posts(project)]  # not the previous combine's
    # the project's own posts file: offered on its card, as the bot's
    out = Prompt.ask("[bold red]куда сохранить[/]", default=scraped_files.combined_output(project))
    dedup = Prompt.ask("[bold red]столбцы для удаления повторов[/]", default="Group,Message ID")
    analysis.combine(inp, out, [c.strip() for c in dedup.split(",")])


def _comments(project):
    inp = _posts_file(project)
    out = Prompt.ask("[bold red]куда сохранить[/]", default=output_path(inp, "comments"))
    fmt = Prompt.ask("[bold red]формат[/]", choices=["parquet", "excel"], default="parquet")
    analysis.explode_comments(inp, out, fmt)


def _participants(project):
    inp = _posts_file(project)
    # the scrape's own name: the rebuilt base (Owner ID kept) replaces it for the mailing
    out = Prompt.ask("[bold red]куда сохранить[/]", default=output_path(inp, "participants"))
    reactors = Prompt.ask("[bold red]файл реакций (пусто — найти автоматически)[/]", default="")
    # parquet only: an excel base can't be picked for the mailing
    analysis.participants(inp, out, reactors or None, "parquet")


def _summary(project):
    inp = _posts_file(project)
    base = Prompt.ask("[bold red]префикс выходных файлов[/]", default=output_path(inp, "summary"))
    analysis.summary(inp, base, "Date", "Group", "Comments")


def _sample(project):
    inp = _posts_file(project)
    out = Prompt.ask("[bold red]куда сохранить[/]", default=output_path(inp, "sample"))
    size = TelethonFunction.ask_int("[bold red]размер выборки[/]", default=10000, min_value=1)
    analysis.sample(inp, out, "Content", "Group", size, 20)


def _filter(project):
    inp = _posts_file(project)
    out = Prompt.ask("[bold red]префикс выходных файлов[/]", default=output_path(inp, "keywords"))
    keywords = Prompt.ask("[bold red]слова через запятую[/]")
    analysis.filter_keywords(
        inp, out, "Content",
        [k.strip() for k in keywords.split(",") if k.strip()], 1_000_000,
    )


def _links(project):
    inp = _posts_file(project)
    out = Prompt.ask("[bold red]куда сохранить[/]", default=output_path(inp, "links"))
    analysis.links(inp, out)


def _read(project):
    files = scraped_files.project_files(project)
    inp = TelethonFunction.ask_file("[bold red]файл[/]", files)
    head = TelethonFunction.ask_int("[bold red]сколько строк показать[/]", default=10, min_value=1)
    df = read_table(inp)
    console.print(df.head(head).to_string())
    console.print(f"[{len(df)} строк x {len(df.columns)} столбцов]")
    # not the file's own format: the conversion is written next to it under its name, over the file itself
    own = Path(inp).suffix.lower().lstrip(".")
    to = Prompt.ask("[bold red]сконвертировать в (пусто — нет)[/]",
                    choices=["", *(f for f in ("parquet", "xlsx", "excel", "csv")
                                   if {"excel": "xlsx"}.get(f, f) != own)],
                    default="")
    if to:
        out = save_table(df, Path(inp).with_suffix(""), to)
        console.print(f"[bold green]Сконвертировано:[/] {out}")


# the tool table's (modules/analysis_tools.py) tools, in the bot's order and words; the terminal asks
# each one's own settings
_ASK = {"links": _links, "filter": _filter, "read": _read, "combine": _combine, "comments": _comments,
        "summary": _summary, "sample": _sample, "participants": _participants}
_TOOLS = [*analysis_tools.MAIN_TOOLS, *analysis_tools.MORE_TOOLS]


class ScraperAnalysisFunc(TelethonFunction):
    """Analyze scraped data"""

    def execute(self, project):
        for index, tool in enumerate(_TOOLS):
            label, hint, *_ = analysis_tools.TOOLS[tool]
            console.print(f"[{index + 1}] {label} — {hint}", style="bold white", markup=False, highlight=False)

        choice = Prompt.ask("[bold magenta]инструмент[/]")
        if not choice.isdigit():
            return

        choice = int(choice) - 1
        if choice < 0 or choice >= len(_TOOLS):
            return

        try:
            _ASK[_TOOLS[choice]](project)
        except SystemExit as err:
            console.print(f"[bold red]Ошибка:[/] {err}")
