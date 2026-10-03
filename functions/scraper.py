from pathlib import Path

from modules.console import console
from rich.prompt import Confirm, Prompt

from functions.base import TelethonFunction
from modules.scraper_creds import TUI_RESUME_HINT, build_credentials, pick_session

from scraper.scrape import ScrapeParams, check_date_range, parse_channels, parse_date, run as scrape_run
from scraper.verify import VerifyParams, run as verify_run


class ScrapeFunc(TelethonFunction):
    """Scrape channel/group"""

    def execute(self):
        account = pick_session(self.storage)
        if account is None:
            return

        channels = parse_channels(Prompt.ask("[bold red]channels (comma-separated)[/]"))
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
                resume_hint=TUI_RESUME_HINT,
            )
            check_date_range(params.date_min, params.date_max, date_min, date_max)
            scrape_run(build_credentials(account), params)
        except SystemExit as err:
            # an int code (1) means the run already printed why it stopped + the resume hint
            if isinstance(err.code, str):
                console.print(f"[bold red]Scrape stopped:[/] {err}")


class VerifyFunc(TelethonFunction):
    """Verify scrape against live channel"""

    def execute(self):
        account = pick_session(self.storage)
        if account is None:
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
            check_date_range(params.date_min, params.date_max, date_min, date_max)
            verify_run(build_credentials(account), params)
        except SystemExit as err:
            # an int code (1) means verify already printed its RESULT / interruption above
            if isinstance(err.code, str):
                console.print(f"[bold red]Verify stopped:[/] {err}")
