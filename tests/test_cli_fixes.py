"""Offline tests for small CLI fixes: the report's first account, the accounts list's phone,
the joiner's and the instant broadcast's CLI flows (functions/report.py, accounts.py, joiner.py,
broadcast_instant.py)."""

import asyncio
import contextlib
import types

import functions.accounts as accounts
import functions.joiner as joiner
import functions.report as report
from functions.accounts import AccountsFunc
from functions.joiner import JoinerFunc
from functions.report import ReportFunc


def ns(**kw):
    return types.SimpleNamespace(**kw)


class _Storage:
    initialize = True
    jsessions_paths = {}

    def __init__(self, sessions=()):
        self.sessions = list(sessions)

    @contextlib.asynccontextmanager
    async def ainitialize_session(self, session):
        yield

    def get_session_path(self, session):
        return None


def _settings():
    return ns(delay=[0], messages=["m"], trigger="", messages_count=1, api_id=1, api_hash="h")


def test_report_first_account_get_me_failure_is_reported(monkeypatch):
    class Dead:
        async def get_me(self):
            raise ConnectionError("proxy down")

    printed = []
    answers = iter(["t.me/chan", "5"])
    monkeypatch.setattr(report.Prompt, "ask", lambda *a, **k: next(answers))
    monkeypatch.setattr(report, "console", ns(input=lambda *a: "", print=lambda *a, **k: printed.append(a[0])))

    func = ReportFunc(_Storage([Dead()]), _settings())
    asyncio.run(func.execute())  # must not raise
    assert any("не удалось опросить аккаунт" in text and "proxy down" in text for text in printed)


def test_accounts_row_without_a_phone_shows_a_dash():
    func = AccountsFunc(_Storage(), _settings())
    me = ns(first_name="A", last_name=None, username="a", phone=None)
    assert func.row(object(), me)[2] == "—"
    assert func.row(object(), ns(first_name="A", last_name=None, username="a", phone="7999"))[2] == "+7999"


def test_accounts_row_escapes_rich_markup_in_names():
    from rich.console import Console
    from rich.table import Table

    func = AccountsFunc(_Storage(), _settings())
    row = func.row(object(), ns(first_name="Ivan [/]", last_name="[bot]", username="a", phone=None))
    assert row[0] == "Ivan \\[/] \\[bot]"
    table = Table()
    table.add_column("Name")
    table.add_row(row[0])
    console = Console(width=80)
    with console.capture() as capture:
        console.print(table)  # unescaped, "[/]" is a MarkupError and the whole list fails
    assert "Ivan [/] [bot]" in capture.get()

def test_accounts_table_shows_the_spambot_status_restricted_last(monkeypatch):
    from modules import restricted_workers

    class Worker:
        def __init__(self, name):
            self.path = f"sessions/{name}.jsession"
            self.me = ns(first_name=name, last_name=None, username=name, phone=None)

    class Storage(_Storage):
        def get_session_path(self, session):
            return session.path

        async def fetch_me(self, client):
            return client.me

    # by username alone they would stay a, b, c, d
    forever, until, clean, new = Worker("a"), Worker("b"), Worker("c"), Worker("d")
    restricted_workers.save([forever.path])
    restricted_workers.save_status({until.path: "12 Nov 2026 [b]", clean.path: "active"})
    printed = []
    monkeypatch.setattr(accounts, "console", ns(status=lambda *a: contextlib.nullcontext(),
                                                print=lambda *a, **k: printed.append(a[0]),
                                                input=lambda *a: ""))  # Enter: no card

    asyncio.run(AccountsFunc(Storage([forever, until, clean, new]), _settings()).execute())

    table = printed[0]
    assert table.title == "Аккаунтов: 4 · ограничены: 2"
    assert [column.header for column in table.columns][-1] == "SpamBot"
    assert list(table.columns[2].cells) == ["@c", "@d", "@b", "@a"]
    # the date is @SpamBot's text: escaped, so "[b]" isn't taken for Rich markup
    assert list(table.columns[-1].cells) == [
        "✅ без ограничений", "❔ не проверялся", "🚫 ЛС ограничены до 12 Nov 2026 \\[b]",
        "⛔ ограничен бессрочно",
    ]


def test_joiner_asks_as_the_bot_and_runs_every_worker(monkeypatch):
    inputs = iter(["3", "1", "https://t.me/chat"])  # a mode that isn't one is asked again; the link
    monkeypatch.setattr(joiner, "console", ns(input=lambda *a: next(inputs), print=lambda *a, **k: None))
    monkeypatch.setattr(joiner.Confirm, "ask", lambda *a, **k: True)  # the captcha
    func = JoinerFunc(_Storage(["a", "b"]), _settings())
    calls = []

    async def run(mode, link, report, captcha=False):
        calls.append((mode, link, captcha, list(func.sessions)))

    func.run = run
    asyncio.run(func.execute())
    assert calls == [("1", "https://t.me/chat", True, ["a", "b"])]


def test_broadcast_ask_offers_only_the_callers_modes(monkeypatch):
    import functions.broadcast as broadcast

    shown = []
    monkeypatch.setattr(broadcast, "console", ns(input=lambda *a: "2", print=lambda text, **k: shown.append(text)))
    monkeypatch.setattr(broadcast.Prompt, "ask", lambda *a, **k: "0")
    monkeypatch.setattr(broadcast.Confirm, "ask", lambda *a, **k: False)
    func = broadcast.Broadcast(_Storage(), _settings())

    assert func.ask(modes=(0, 4)) == 4  # «2» is the second offered mode: the stickers
    assert len(shown) == 2 and "с медиа" not in " ".join(shown)


def test_instant_broadcast_runs_every_worker_through_run(monkeypatch):
    import functions.broadcast as broadcast
    import functions.broadcast_instant as instant

    def ask(self, modes=None):
        self.choice = 4
        self.sticker_set = "Pack"

    monkeypatch.setattr(instant, "console", ns(input=lambda *a: "https://t.me/chat"))
    monkeypatch.setattr(broadcast.Broadcast, "ask", ask)
    monkeypatch.setattr(instant, "ask_run_options", lambda settings: ("go", 0))
    monkeypatch.setattr(instant, "save_run_options", lambda settings, options: True)
    func = instant.InstantBroadcastFunc(_Storage(["a", "b"]), _settings())
    calls = []

    async def run(*args):
        calls.append(args[:-1])

    func.run = run
    asyncio.run(func.execute())

    assert calls == [(4, False, None, "Pack", None, "https://t.me/chat")]
