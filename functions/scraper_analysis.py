from pathlib import Path

from rich.console import Console
from rich.prompt import Prompt

from functions.base import TelethonFunction

from scraper import analysis
from scraper.datafiles import read_table, save_table

console = Console()


def _combine():
    inp = Prompt.ask("[bold red]input (file / dir / glob of *.parquet)[/]")
    out = Prompt.ask("[bold red]output[/]")
    dedup = Prompt.ask("[bold red]dedup columns[/]", default="Group,Message ID")
    analysis.combine(inp, out, [c.strip() for c in dedup.split(",")])


def _comments():
    inp = Prompt.ask("[bold red]input posts file[/]")
    out = Prompt.ask("[bold red]output[/]")
    fmt = Prompt.ask("[bold red]format[/]", choices=["parquet", "excel"], default="parquet")
    analysis.explode_comments(inp, out, fmt)


def _participants():
    inp = Prompt.ask("[bold red]input posts file[/]")
    out = Prompt.ask("[bold red]output[/]")
    reactors = Prompt.ask("[bold red]reactors file (blank = auto)[/]", default="")
    fmt = Prompt.ask("[bold red]format[/]", choices=["parquet", "excel"], default="parquet")
    analysis.participants(inp, out, reactors or None, fmt)


def _summary():
    inp = Prompt.ask("[bold red]input[/]")
    base = Prompt.ask("[bold red]output base (prefix)[/]")
    analysis.summary(inp, base, "Date", "Group", "Comments")


def _sample():
    inp = Prompt.ask("[bold red]input[/]")
    out = Prompt.ask("[bold red]output[/]")
    size = int(Prompt.ask("[bold red]sample size[/]", default="10000"))
    analysis.sample(inp, out, "Content", "Group", size, 20)


def _filter():
    inp = Prompt.ask("[bold red]input[/]")
    out = Prompt.ask("[bold red]output base[/]")
    keywords = Prompt.ask("[bold red]keywords (comma-separated)[/]")
    analysis.filter_keywords(
        inp, out, "Content",
        [k.strip() for k in keywords.split(",") if k.strip()], 1_000_000,
    )


def _links():
    inp = Prompt.ask("[bold red]input[/]")
    out = Prompt.ask("[bold red]output[/]")
    analysis.links(inp, out)


def _read():
    inp = Prompt.ask("[bold red]input[/]")
    head = int(Prompt.ask("[bold red]rows to show[/]", default="10"))
    df = read_table(inp)
    console.print(df.head(head).to_string())
    console.print(f"[{len(df)} rows x {len(df.columns)} columns]")
    to = Prompt.ask("[bold red]convert to (blank = no)[/]",
                    choices=["", "parquet", "xlsx", "excel", "csv"], default="")
    if to:
        out = save_table(df, Path(inp).with_suffix(""), to)
        console.print(f"[bold green]Converted:[/] {out}")


_TOOLS = [
    ("combine — merge parquet files, drop duplicates", _combine),
    ("comments — flatten Comments List (one row per comment)", _comments),
    ("participants — unique ID + username + access hash + name", _participants),
    ("summary — per-group monthly tables", _summary),
    ("sample — proportional per-category sample to xlsx", _sample),
    ("filter — keep rows matching keywords", _filter),
    ("links — extract and count t.me links", _links),
    ("read — print the head of a data file, optionally convert", _read),
]


class ScraperAnalysisFunc(TelethonFunction):
    """Analyze scraped data"""

    def execute(self):
        for index, (label, _) in enumerate(_TOOLS):
            console.print(f"[bold white][{index + 1}] {label}[/]")

        choice = Prompt.ask("[bold magenta]tool[/]")
        if not choice.isdigit():
            return

        choice = int(choice) - 1
        if choice < 0 or choice >= len(_TOOLS):
            return

        try:
            _TOOLS[choice][1]()
        except SystemExit as err:
            console.print(f"[bold red]Error:[/] {err}")
