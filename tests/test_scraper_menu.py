"""Offline tests for the scraper menu plugins: drive the input() flow with scripted
prompts and capture the scraper calls, without network or Telegram."""

from pathlib import Path

import pytest

from functions import scraper, scraper_analysis
from scraper import analysis


class FakeSettings:
    api_id = 42
    api_hash = "h"


class FakeStorage:
    def __init__(self, sessions=()):
        self.sessions = list(sessions)


def _answers(monkeypatch, module, texts, bools=()):
    """Make Prompt.ask / Confirm.ask in `module` return scripted values in order."""
    text_it = iter(texts)
    bool_it = iter(bools)
    monkeypatch.setattr(module.Prompt, "ask", lambda *a, **k: next(text_it))
    if hasattr(module, "Confirm"):
        monkeypatch.setattr(module.Confirm, "ask", lambda *a, **k: next(bool_it))


def test_scrape_func_builds_params_from_prompts(monkeypatch):
    captured = {}
    monkeypatch.setattr(scraper, "pick_session_string", lambda storage: "SESSION")
    monkeypatch.setattr(scraper, "scrape_run", lambda creds, params: captured.update(creds=creds, params=params))

    _answers(
        monkeypatch, scraper,
        texts=["@a, @b", "Test", "out/dir", "01.01.2024", "31.01.2024", "", "500", "parquet"],
        bools=[True, False, True, False],
    )

    scraper.ScrapeFunc(FakeStorage(), FakeSettings()).execute()

    p = captured["params"]
    assert p.channels == ["@a", "@b"]
    assert p.name == "Test" and p.out_dir == Path("out/dir")
    assert p.max_messages == 500 and p.fmt == "parquet"
    assert (p.with_comments, p.with_reactors, p.with_participants, p.resume) == (True, False, True, False)
    assert p.date_min.strftime("%d.%m.%Y") == "01.01.2024"
    assert (p.date_max.hour, p.date_max.minute, p.date_max.second) == (23, 59, 59)  # end_of_day
    assert captured["creds"].api_id == 42 and captured["creds"].session_string == "SESSION"


def test_scrape_func_stops_on_empty_channels(monkeypatch):
    called = []
    monkeypatch.setattr(scraper, "pick_session_string", lambda storage: "SESSION")
    monkeypatch.setattr(scraper, "scrape_run", lambda *a: called.append(a))
    _answers(monkeypatch, scraper, texts=["   ,  "])  # only the channels prompt is reached

    scraper.ScrapeFunc(FakeStorage(), FakeSettings()).execute()
    assert called == []  # no run attempted


def test_verify_func_builds_params_from_prompts(monkeypatch):
    captured = {}
    monkeypatch.setattr(scraper, "pick_session_string", lambda storage: "SESSION")
    monkeypatch.setattr(scraper, "verify_run", lambda creds, params: captured.update(params=params))

    _answers(
        monkeypatch, scraper,
        texts=["posts.parquet", "@a", "01.01.2024", "31.01.2024", "missed.parquet", "5"],
    )

    scraper.VerifyFunc(FakeStorage(), FakeSettings()).execute()

    p = captured["params"]
    assert p.input == "posts.parquet" and p.channel == "@a"
    assert p.output == "missed.parquet" and p.comment_sample == 5
    assert p.date_min.strftime("%d.%m.%Y") == "01.01.2024"


def test_analysis_dispatch_combine(monkeypatch):
    captured = {}
    monkeypatch.setattr(analysis, "combine",
                        lambda inp, out, cols: captured.update(args=(inp, out, cols)))
    _answers(monkeypatch, scraper_analysis, texts=["1", "in", "out", "Group,Message ID"])

    scraper_analysis.ScraperAnalysisFunc(FakeStorage(), FakeSettings()).execute()
    assert captured["args"] == ("in", "out", ["Group", "Message ID"])


def test_analysis_participants_blank_reactors_is_none(monkeypatch):
    captured = {}
    monkeypatch.setattr(analysis, "participants",
                        lambda inp, out, reactors, fmt: captured.update(reactors=reactors))
    # tool 3 = participants; reactors answer is blank -> None
    _answers(monkeypatch, scraper_analysis, texts=["3", "in", "out", "", "parquet"])

    scraper_analysis.ScraperAnalysisFunc(FakeStorage(), FakeSettings()).execute()
    assert captured["reactors"] is None


def test_pick_session_string_returns_chosen_account(monkeypatch):
    from modules import scraper_creds

    class FakeSession:
        def save(self):
            return "KEY"

    class FakeClient:
        session = FakeSession()

    storage = FakeStorage([FakeClient()])
    storage.get_session_path = lambda client: "sessions/acc.jsession"
    monkeypatch.setattr(scraper_creds.Prompt, "ask", lambda *a, **k: "1")

    assert scraper_creds.pick_session_string(storage) == "KEY"


def test_pick_session_string_none_on_empty_storage():
    from modules import scraper_creds

    assert scraper_creds.pick_session_string(FakeStorage([])) is None
