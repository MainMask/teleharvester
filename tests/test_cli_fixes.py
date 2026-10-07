"""Offline tests for small CLI fixes: the report's first account, the accounts list's phone,
and the joiner's single-loop join (functions/report.py, accounts.py, joiner.py)."""

import asyncio
import contextlib
import types

import pytest

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
    return ns(delay=[0], messages=["m"], trigger="", messages_count=1)


def test_report_first_account_get_me_failure_is_reported(monkeypatch):
    class Dead:
        async def get_me(self):
            raise ConnectionError("proxy down")

    printed = []
    answers = iter(["t.me/chan", "5"])
    monkeypatch.setattr(report.Prompt, "ask", lambda *a, **k: next(answers))
    monkeypatch.setattr(report, "console", ns(input=lambda *a: "", print=lambda *a, **k: printed.append(a[0])))

    func = ReportFunc(_Storage([Dead()]), _settings())
    func.ask_accounts_count = lambda: None
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
                                                print=lambda *a, **k: printed.append(a[0])))

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


class _Worker:
    def __init__(self, name):
        self.name = name

    async def start(self):
        return self


def _run_joiner(monkeypatch, broadcast_choice, joined):
    """Drive JoinerFunc.execute (speed normal, no captcha) with two workers; `joined` is
    what join() gives each. Returns (printed lines, broadcasts as (worker, target))."""
    printed, sent = [], []
    inputs = iter(["1", "https://t.me/chat"])  # mode, link
    monkeypatch.setattr(joiner, "console", ns(
        input=lambda *a: next(inputs), print=lambda *a, **k: printed.append(" ".join(map(str, a))),
        status=lambda *a: contextlib.nullcontext()))
    monkeypatch.setattr(joiner.Prompt, "ask", lambda *a, **k: "normal")
    confirms = iter([broadcast_choice is not None, False])  # broadcast instantly?, captcha?
    monkeypatch.setattr(joiner.Confirm, "ask", lambda *a, **k: next(confirms))
    monkeypatch.setattr(joiner, "track", lambda iterable, *a, **k: iterable)

    class FakeBroadcast:
        def __init__(self, storage, settings):
            pass

        def ask(self):
            return broadcast_choice

        async def broadcast(self, session, target, report):
            sent.append((session.name, target))

    monkeypatch.setattr(joiner, "Broadcast", FakeBroadcast)

    workers = [_Worker("a"), _Worker("b")]
    func = JoinerFunc(_Storage(workers), _settings())
    func.ask_accounts_count = lambda: None
    func.ask_int = lambda *a, **k: 0
    results = iter(joined)

    async def join(session, link, index, mode, report=None):
        return next(results)

    func.join = join
    asyncio.run(func.execute())
    return printed, sent


def test_broadcast_ask_leaves_the_accounts_count_to_its_caller(monkeypatch):
    # the joiner has asked it already: a second prompt there was ignored
    import functions.broadcast as broadcast

    monkeypatch.setattr(broadcast, "console", ns(input=lambda *a: "1", print=lambda *a, **k: None))
    monkeypatch.setattr(broadcast.Prompt, "ask", lambda *a, **k: "0")
    monkeypatch.setattr(broadcast.Confirm, "ask", lambda *a, **k: False)
    func = broadcast.Broadcast(_Storage(), _settings())
    func.ask_accounts_count = lambda: pytest.fail("Broadcast.ask must not ask the accounts count")

    assert func.ask() == 0


def test_instant_broadcast_asks_the_accounts_count(monkeypatch):
    import functions.broadcast as broadcast
    import functions.broadcast_instant as instant

    asked = []
    monkeypatch.setattr(instant, "console", ns(input=lambda *a: "https://t.me/chat"))
    monkeypatch.setattr(broadcast.Broadcast, "ask", lambda self: 0)
    monkeypatch.setattr(broadcast.Broadcast, "ask_accounts_count", lambda self: asked.append(1))

    asyncio.run(instant.InstantBroadcastFunc(_Storage(), _settings()).execute())

    assert asked == [1]


def test_joiner_without_broadcast(monkeypatch):
    printed, sent = _run_joiner(monkeypatch, None, ["chat-a", False])
    assert sent == []
    assert any("Вступило аккаунтов: 1" in line for line in printed)


def test_joiner_broadcasts_after_all_joined(monkeypatch):
    printed, sent = _run_joiner(monkeypatch, 0, ["chat-a", False])
    # each worker writes to the chat it joined, or the link when it didn't
    assert sent == [("a", "chat-a"), ("b", "https://t.me/chat")]


def test_joiner_single_account_campaign_broadcasts_right_after_each_join(monkeypatch):
    printed, sent = _run_joiner(monkeypatch, 1, ["chat-a", False])
    assert sent == [("a", "chat-a"), ("b", "https://t.me/chat")]
    # only the worker that really joined is reported as joined
    assert sum("Аккаунт вступил" in line for line in printed) == 1
