from pathlib import Path

from modules.console import console
from rich.prompt import Prompt

from functions.base import TelethonFunction

from modules import scraped_files
from modules.scraped_files import output_path
from scraper import analysis
from scraper.datafiles import read_table, save_table


def _posts_file() -> str:
    """A scraped posts file: a number from assets/databases or a typed path."""
    return TelethonFunction.ask_file("[bold red]файл постов[/]", scraped_files.posts_bases())


def _combine():
    inp = Prompt.ask("[bold red]что объединить (файл / папка / шаблон *.parquet)[/]", default=scraped_files.BASES_DIR)
    out = Prompt.ask("[bold red]куда сохранить[/]", default=str(Path(scraped_files.BASES_DIR) / "Combined_posts"))
    dedup = Prompt.ask("[bold red]столбцы для удаления повторов[/]", default="Group,Message ID")
    analysis.combine(inp, out, [c.strip() for c in dedup.split(",")])


def _comments():
    inp = _posts_file()
    out = Prompt.ask("[bold red]куда сохранить[/]", default=output_path(inp, "comments"))
    fmt = Prompt.ask("[bold red]формат[/]", choices=["parquet", "excel"], default="parquet")
    analysis.explode_comments(inp, out, fmt)


def _participants():
    inp = _posts_file()
    # the scrape's own name: the rebuilt base (Owner ID kept) replaces it for the mailing
    out = Prompt.ask("[bold red]куда сохранить[/]", default=output_path(inp, "participants"))
    reactors = Prompt.ask("[bold red]файл реакций (пусто — найти автоматически)[/]", default="")
    # parquet only: an excel base can't be picked for the mailing
    analysis.participants(inp, out, reactors or None, "parquet")


def _summary():
    inp = _posts_file()
    base = Prompt.ask("[bold red]префикс выходных файлов[/]", default=output_path(inp, "summary"))
    analysis.summary(inp, base, "Date", "Group", "Comments")


def _sample():
    inp = _posts_file()
    out = Prompt.ask("[bold red]куда сохранить[/]", default=output_path(inp, "sample"))
    size = TelethonFunction.ask_int("[bold red]размер выборки[/]", default=10000, min_value=1)
    analysis.sample(inp, out, "Content", "Group", size, 20)


def _filter():
    inp = _posts_file()
    out = Prompt.ask("[bold red]префикс выходных файлов[/]", default=output_path(inp, "keywords"))
    keywords = Prompt.ask("[bold red]слова через запятую[/]")
    analysis.filter_keywords(
        inp, out, "Content",
        [k.strip() for k in keywords.split(",") if k.strip()], 1_000_000,
    )


def _links():
    inp = _posts_file()
    out = Prompt.ask("[bold red]куда сохранить[/]", default=output_path(inp, "links"))
    analysis.links(inp, out)


def _read():
    inp = TelethonFunction.ask_file("[bold red]файл[/]", scraped_files.data_files())
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

    def execute(self):
        for index, (label, _) in enumerate(_TOOLS):
            console.print(f"[bold white][{index + 1}] {label}[/]")

        choice = Prompt.ask("[bold magenta]инструмент[/]")
        if not choice.isdigit():
            return

        choice = int(choice) - 1
        if choice < 0 or choice >= len(_TOOLS):
            return

        try:
            _TOOLS[choice][1]()
        except SystemExit as err:
            console.print(f"[bold red]Ошибка:[/] {err}")
