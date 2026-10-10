from pathlib import Path

from modules.console import console
from rich.prompt import Confirm, Prompt

from functions.base import TelethonFunction
from modules import scraped_files
from modules.scraper_creds import (
    build_credentials, find_account, personal_storage, pick_session, scrape_accounts,
)

from scraper.members import MembersParams, run as members_run
from scraper.scrape import (
    ScrapeParams, check_date_range, parse_channels, parse_date, pending_resume, resume_params,
    run as scrape_run,
)
from scraper.verify import VerifyParams, run as verify_run


# a base scraped by an account holds access hashes valid for that account only
PERSONAL_NOTE = ("внимание: это личный аккаунт — большой скрап с реакциями может вызвать временные "
                 "лимиты Telegram на нём, а рассылка воркеров дойдёт до людей из его базы только по "
                 "username (access hash действует только для собравшего аккаунта). Для полной базы "
                 "выберите воркера.")


def _note_personal(account):
    if account.personal:
        console.print(PERSONAL_NOTE, style="bold yellow", markup=False)


class ScrapeFunc(TelethonFunction):
    """Scrape channel/group"""

    def execute(self):
        personal = personal_storage(self.settings.api_id, self.settings.api_hash)
        # always the bases folder: the mailing, contacts, verify and analysis pick their files there
        out_dir = scraped_files.BASES_DIR
        name = Prompt.ask(f"[bold red]имя для выходных файлов (в {out_dir})[/]")

        meta = pending_resume(out_dir, name)
        if meta is not None and Confirm.ask(
            f"[bold red]под этим именем есть незавершённый скрап '{self.safe(name)}' "
            f"({self.safe(', '.join(meta['channels']))}, собрано постов: {meta.get('t_index', 0)}) — "
            "продолжить? (нет — начать заново, собранное удалится)[/]", default=True,
        ):
            params = resume_params(meta, out_dir)
            if params.account:  # the same account: the scraped access hashes are valid for it only
                account = find_account(scrape_accounts(self.storage, personal), params.account)
                if account is None:
                    console.print(f"[bold red]Аккаунт скрапа {self.safe(params.account)} "
                                  "не найден — продолжить скрап можно только на нём.[/]")
                    return
            else:  # a checkpoint from before accounts were stored
                account = pick_session(self.storage, personal)
                if account is None:
                    return
                _note_personal(account)
                params.account = account.path
            self._run(account, params)
            return

        account = pick_session(self.storage, personal)
        if account is None:
            return
        _note_personal(account)

        channels = parse_channels(Prompt.ask("[bold red]каналы через запятую (в приватных аккаунт должен состоять)[/]"))
        if not channels:
            console.print("[bold red]Каналы не указаны.[/]")
            return

        date_min = Prompt.ask("[bold red]дата с (ДД.ММ.ГГГГ или ГГГГ-ММ-ДД)[/]")
        date_max = Prompt.ask("[bold red]дата по (ДД.ММ.ГГГГ или ГГГГ-ММ-ДД)[/]")
        keyword = Prompt.ask("[bold red]фильтр по слову (необязательно)[/]", default="")
        max_messages = self.ask_int("[bold red]максимум постов[/]", default=1000000, min_value=1)
        with_comments = Confirm.ask("[bold red]собирать комментарии?[/]", default=True)
        with_reactors = Confirm.ask("[bold red]собирать реакции? (медленно)[/]", default=True)
        with_participants = Confirm.ask("[bold red]собрать базу участников?[/]", default=True)

        try:
            params = ScrapeParams(
                channels=channels,
                date_min=parse_date(date_min),
                date_max=parse_date(date_max, end_of_day=True),
                name=name,
                keyword=keyword,
                max_messages=max_messages,
                out_dir=Path(out_dir),
                with_comments=with_comments,
                with_reactors=with_reactors,
                with_participants=with_participants,
                account=account.path,
            )
            check_date_range(params.date_min, params.date_max, date_min, date_max)
        except SystemExit as err:
            console.print(f"[bold red]Скрап остановлен:[/] {err}")
            return
        self._run(account, params)

    @staticmethod
    def _run(account, params):
        try:
            scrape_run(build_credentials(account.client), params)
        except SystemExit as err:
            # an int code (1) means the run already printed why it stopped + how to continue
            if isinstance(err.code, str):
                console.print(f"[bold red]Скрап остановлен:[/] {err}")


class VerifyFunc(TelethonFunction):
    """Verify scrape against live channel"""

    def execute(self):
        account = pick_session(self.storage, personal_storage(self.settings.api_id, self.settings.api_hash))
        if account is None:
            return

        input_path = self.ask_file("[bold red]файл постов скрапа[/]", scraped_files.posts_bases())
        # the file's channels and the scrape's own window, when the scrape stored it
        channels, window = scraped_files.verify_presets(input_path)
        if len(channels) > 1:
            console.print(f"[bold white]каналы в файле:[/] {', '.join(channels)}")
        channel = Prompt.ask("[bold red]канал (@name / t.me / числовой id)[/]",
                             default=channels[0] if channels else None)
        date_min = Prompt.ask("[bold red]дата с (как в скрапе)[/]", default=window[0] if window else None)
        date_max = Prompt.ask("[bold red]дата по (как в скрапе)[/]", default=window[1] if window else None)
        output = Prompt.ask("[bold red]файл результата (необязательно)[/]", default="")
        comment_sample = self.ask_int("[bold red]сколько веток комментариев перепроверить[/]", default=0, min_value=0)

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
            verify_run(build_credentials(account.client), params)
        except SystemExit as err:
            # an int code (1) means verify already printed its RESULT / interruption above
            if isinstance(err.code, str):
                console.print(f"[bold red]Проверка остановлена:[/] {err}")


class MembersFunc(TelethonFunction):
    """Export group members"""

    def execute(self):
        account = pick_session(self.storage, personal_storage(self.settings.api_id, self.settings.api_hash))
        if account is None:
            return
        _note_personal(account)

        chats = parse_channels(Prompt.ask("[bold red]группы через запятую[/]"))
        if not chats:
            console.print("[bold red]Группы не указаны.[/]")
            return

        out_dir = scraped_files.BASES_DIR  # the bases folder, where the mailing picks its bases
        name = Prompt.ask(f"[bold red]имя для выходных файлов (в {out_dir})[/]")
        try:
            path, _ = members_run(build_credentials(account.client),
                                  MembersParams(chats=chats, name=name, out_dir=Path(out_dir)))
        except SystemExit as err:  # e.g. the account's session is no longer authorized
            console.print(f"[bold red]Сбор участников остановлен:[/] {err}")
            return
        if path is None:
            console.print("[bold red]Ни одного участника не собрано.[/]")
