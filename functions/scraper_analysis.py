from pathlib import Path

from modules.console import console
from rich.prompt import Prompt

from functions.base import TelethonFunction

from modules import scraped_files
from modules.scraped_files import output_path
from scraper import analysis
from scraper.datafiles import read_table, save_table


# project: a project's name when run from its card (functions/projects.py): its files are offered only

def _posts_file(project) -> str:
    """A scraped posts file: a number from assets/databases or a typed path."""
    files = scraped_files.project_files(project, kinds=("posts",)) if project else scraped_files.posts_bases()
    return TelethonFunction.ask_file("[bold red]файл постов[/]", files)


def _combine(project):
    if project:  # blank: all the project's posts files, as the bot's «🗂 Все файлы постов»
        inp = Prompt.ask("[bold red]что объединить (файл / папка / шаблон *.parquet; пусто — все файлы постов "
                         "проекта)[/]", default="")
        inp = inp or [path for path, _ in scraped_files.project_files(project, kinds=("posts",))]
    else:  # the posts files only, as the bot's button: the folder's bases would be read just to be skipped
        inp = Prompt.ask("[bold red]что объединить (файл / папка / шаблон *.parquet)[/]",
                         default=str(Path(scraped_files.BASES_DIR) / "*_posts*.parquet"))
    out = Prompt.ask("[bold red]куда сохранить[/]", default=str(Path(scraped_files.BASES_DIR) / "Combined_posts"))
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
    files = scraped_files.project_files(project) if project else scraped_files.data_files()
    inp = TelethonFunction.ask_file("[bold red]файл[/]", files)
    head = TelethonFunction.ask_int("[bold red]сколько строк показать[/]", default=10, min_value=1)
    df = read_table(inp)
    console.print(df.head(head).to_string())
    console.print(f"[{len(df)} строк x {len(df.columns)} столбцов]")
    to = Prompt.ask("[bold red]сконвертировать в (пусто — нет)[/]",
                    choices=["", "parquet", "xlsx", "excel", "csv"], default="")
    if to:
        out = save_table(df, Path(inp).with_suffix(""), to)
        console.print(f"[bold green]Сконвертировано:[/] {out}")


# the bot's order and words (bot/routers/scraping.py ANALYSIS), plus the participants rebuild
_TOOLS = [
    ("🔗 Ссылки на другие каналы — найдёт в постах ссылки на Telegram-каналы и посчитает, "
     "сколько раз упоминался каждый", _links),
    ("🔎 Поиск постов по словам — оставит только посты, где есть хотя бы одно из ваших слов", _filter),
    ("👀 Посмотреть файл — первые строки любого файла, можно сконвертировать в xlsx/csv", _read),
    ("🧩 Объединить файлы постов — склеит несколько файлов постов в один и уберёт повторы", _combine),
    ("💬 Комментарии одной таблицей — одна строка на комментарий", _comments),
    ("📊 Активность по месяцам — сколько постов и комментариев было в каждом канале", _summary),
    ("🎲 Случайная выборка постов — пропорционально по каналам, для ручного просмотра", _sample),
    ("👥 Пересобрать базу участников — из файла постов; Owner ID сохраняется", _participants),
]


class ScraperAnalysisFunc(TelethonFunction):
    """Analyze scraped data"""

    def execute(self, project=None):
        for index, (label, _) in enumerate(_TOOLS):
            console.print(f"[bold white][{index + 1}] {label}[/]")

        choice = Prompt.ask("[bold magenta]инструмент[/]")
        if not choice.isdigit():
            return

        choice = int(choice) - 1
        if choice < 0 or choice >= len(_TOOLS):
            return

        try:
            _TOOLS[choice][1](project)
        except SystemExit as err:
            console.print(f"[bold red]Ошибка:[/] {err}")
