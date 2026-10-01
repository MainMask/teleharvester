import re
from pathlib import Path

from rich.console import Console
from rich.prompt import Confirm, Prompt

from functions.base import TelethonFunction
from modules.scraper_creds import build_credentials, pick_session_string

from scraper.scrape import ScrapeParams, parse_date, run as scrape_run
from scraper.verify import VerifyParams, run as verify_run

console = Console()


def _channels(raw: str) -> list[str]:
    return [c.strip() for c in re.split(r"[,\s]+", raw) if c.strip()]


class ScrapeFunc(TelethonFunction):
    """Scrape channel/group"""

    def execute(self):
        session_string = pick_session_string(self.storage)
        if session_string is None:
            return

        channels = _channels(Prompt.ask("[bold red]channels (comma-separated)[/]"))
        if not channels:
            console.print("[bold red]No channels given.[/]")
            return

        name = Prompt.ask("[bold red]output name[/]")
        out_dir = Prompt.ask("[bold red]output dir[/]", default="assets/databases")
        date_min = Prompt.ask("[bold red]date-min (DD.MM.YYYY or YYYY-MM-DD)[/]")
        date_max = Prompt.ask("[bold red]date-max (DD.MM.YYYY or YYYY-MM-DD)[/]")
        keyword = Prompt.ask("[bold red]keyword (optional)[/]", default="")
        max_messages = self.ask_int("[bold red]max messages[/]", default=1000000, min_value=1)
        fmt = Prompt.ask("[bold red]format[/]", choices=["parquet", "excel"], default="parquet")
        with_comments = Confirm.ask("[bold red]fetch comments?[/]", default=True)
        with_reactors = Confirm.ask("[bold red]fetch reactors? (slow)[/]", default=True)
        with_participants = Confirm.ask("[bold red]build participants table?[/]", default=True)
        resume = Confirm.ask("[bold red]resume an interrupted run?[/]", default=False)

        try:
            params = ScrapeParams(
                channels=channels,
                date_min=parse_date(date_min),
                date_max=parse_date(date_max, end_of_day=True),
                name=name,
                keyword=keyword,
                max_messages=max_messages,
                fmt=fmt,
                out_dir=Path(out_dir),
                with_comments=with_comments,
                with_reactors=with_reactors,
                with_participants=with_participants,
                resume=resume,
            )
            scrape_run(build_credentials(self.settings, session_string), params)
        except SystemExit as err:
            console.print(f"[bold red]Scrape stopped:[/] {err}")


class VerifyFunc(TelethonFunction):
    """Verify scrape against live channel"""

    def execute(self):
        session_string = pick_session_string(self.storage)
        if session_string is None:
            return

        input_path = Prompt.ask("[bold red]scraped posts file[/]")
        channel = Prompt.ask("[bold red]channel (@name / t.me URL / numeric id)[/]")
        date_min = Prompt.ask("[bold red]date-min (same as scrape)[/]")
        date_max = Prompt.ask("[bold red]date-max (same as scrape)[/]")
        output = Prompt.ask("[bold red]output file (optional)[/]", default="")
        comment_sample = self.ask_int("[bold red]comment threads to re-check[/]", default=0, min_value=0)

        try:
            params = VerifyParams(
                input=input_path,
                channel=channel,
                date_min=parse_date(date_min),
                date_max=parse_date(date_max, end_of_day=True),
                output=output,
                comment_sample=comment_sample,
            )
            verify_run(build_credentials(self.settings, session_string), params)
        except SystemExit as err:
            console.print(f"[bold red]Verify stopped:[/] {err}")
