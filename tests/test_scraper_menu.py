"""Offline tests for the scraper menu plugins: drive the input() flow with scripted
prompts and capture the scraper calls, without network or Telegram."""

from pathlib import Path

from functions import scraper, scraper_analysis
from scraper import analysis


class FakeSettings:
    api_id = 42
    api_hash = "h"


class _FakeSession:
    def save(self):
        return "SESSION"


# a teleharvester account's client: the scraper reuses its key, app id and proxy
_INIT = type("FakeInit", (), {"device_model": "Pixel", "system_version": "SDK 33",
                              "app_version": "10.0", "lang_code": "en", "system_lang_code": "en"})()
_ACCOUNT = type("FakeAccount", (), {"api_id": 42, "api_hash": "h", "session": _FakeSession(),
                                    "_proxy": ("socks5", "1.2.3.4", 1080), "_init_request": _INIT})()


# what pick_session returns: the account's session path rides along to the checkpoint
_PICKED = None  # set below, once _ACCOUNT exists


class FakeStorage:
    def __init__(self, sessions=()):
        self.sessions = list(sessions)


def _picked():
    from modules.scraper_creds import ScrapeAccount
    return ScrapeAccount("sessions/acc.jsession", _ACCOUNT, "Acc", False)


_PICKED = _picked()


def _answers(monkeypatch, module, texts, bools=()):
    """Make Prompt.ask / Confirm.ask in `module` return scripted values in order."""
    text_it = iter(texts)
    bool_it = iter(bools)
    monkeypatch.setattr(module.Prompt, "ask", lambda *a, **k: next(text_it))
    if hasattr(module, "Confirm"):
        monkeypatch.setattr(module.Confirm, "ask", lambda *a, **k: next(bool_it))


def test_scrape_func_builds_params_from_prompts(monkeypatch):
    captured = {}
    monkeypatch.setattr(scraper, "pick_session", lambda storage, personal=None: _PICKED)
    monkeypatch.setattr(scraper, "scrape_run", lambda creds, params: captured.update(creds=creds, params=params))

    _answers(
        monkeypatch, scraper,
        texts=["Test", "out/dir", "@a, @b", "01.01.2024", "31.01.2024", "", "500"],
        bools=[True, False, True],
    )

    scraper.ScrapeFunc(FakeStorage(), FakeSettings()).execute()

    p = captured["params"]
    assert p.channels == ["@a", "@b"]
    assert p.name == "Test" and p.out_dir == Path("out/dir")
    assert p.max_messages == 500
    assert (p.with_comments, p.with_reactors, p.with_participants, p.resume) == (True, False, True, False)
    assert p.date_min.strftime("%d.%m.%Y") == "01.01.2024"
    assert (p.date_max.hour, p.date_max.minute, p.date_max.second) == (23, 59, 59)  # end_of_day
    assert captured["creds"].api_id == 42 and captured["creds"].session_string == "SESSION"
    assert captured["creds"].proxy == ("socks5", "1.2.3.4", 1080)  # the account's own proxy
    assert captured["creds"].device["device_model"] == "Pixel"     # ...and its device


def test_scrape_func_stops_on_empty_channels(monkeypatch):
    called = []
    monkeypatch.setattr(scraper, "pick_session", lambda storage, personal=None: _PICKED)
    monkeypatch.setattr(scraper, "scrape_run", lambda *a: called.append(a))
    _answers(monkeypatch, scraper, texts=["Test", "out/dir", "   ,  "])  # stops at the channels

    scraper.ScrapeFunc(FakeStorage(), FakeSettings()).execute()
    assert called == []  # no run attempted


def test_scrape_func_continues_an_interrupted_scrape(monkeypatch, tmp_path):
    import json

    ckpt = tmp_path / "Test_partial" / "checkpoint"
    ckpt.mkdir(parents=True)
    (ckpt / "resume.json").write_text(json.dumps({
        "name": "Test", "channels": ["@a"], "keyword": "war", "t_index": 7,
        "date_min": "2024-01-01T00:00:00+00:00", "date_max": "2024-01-31T23:59:59+00:00",
        "with_reactors": False, "max_messages": 50, "channel_index": 0, "last_id": 5}))
    captured = {}
    monkeypatch.setattr(scraper, "pick_session", lambda storage, personal=None: _PICKED)
    monkeypatch.setattr(scraper, "scrape_run", lambda creds, params: captured.update(params=params))
    _answers(monkeypatch, scraper, texts=["Test", str(tmp_path)], bools=[True])  # no other questions

    scraper.ScrapeFunc(FakeStorage(), FakeSettings()).execute()

    p = captured["params"]
    assert p.resume and p.channels == ["@a"] and p.keyword == "war"
    assert (p.with_reactors, p.max_messages, p.out_dir) == (False, 50, tmp_path)
    assert p.date_max.strftime("%d.%m.%Y %H:%M") == "31.01.2024 23:59"


def test_verify_func_builds_params_from_prompts(monkeypatch):
    captured = {}
    monkeypatch.setattr(scraper, "pick_session", lambda storage, personal=None: _PICKED)
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
    _answers(monkeypatch, scraper_analysis, texts=["4", "in", "out", "Group,Message ID"])  # 4 = combine

    scraper_analysis.ScraperAnalysisFunc(FakeStorage(), FakeSettings()).execute()
    assert captured["args"] == ("in", "out", ["Group", "Message ID"])


def test_analysis_participants_blank_reactors_is_none(monkeypatch):
    captured = {}
    monkeypatch.setattr(analysis, "participants",
                        lambda inp, out, reactors, fmt: captured.update(reactors=reactors, fmt=fmt))
    # tool 8 = participants; reactors answer is blank -> None; no format question
    _answers(monkeypatch, scraper_analysis, texts=["8", "in", "out", ""])

    scraper_analysis.ScraperAnalysisFunc(FakeStorage(), FakeSettings()).execute()
    assert captured == {"reactors": None, "fmt": "parquet"}


def test_pick_session_returns_chosen_account(monkeypatch):
    from modules import scraper_creds

    client = object()
    storage = FakeStorage([client])
    storage.get_session_path = lambda client: "sessions/acc.jsession"
    storage.jsessions_paths = {}  # a plain .session: labelled by its file name
    monkeypatch.setattr(scraper_creds.Prompt, "ask", lambda *a, **k: "1")

    picked = scraper_creds.pick_session(storage)
    assert picked.client is client and picked.path == "sessions/acc.jsession" and not picked.personal


def test_pick_session_none_on_empty_storage():
    from modules import scraper_creds

    assert scraper_creds.pick_session(FakeStorage([])) is None


# --- CLI pickers: the scrape's files by number, defaults as in the bot ----------------------

def _scraped(tmp_path, monkeypatch, window=None):
    import pandas as pd
    from modules import scraped_files

    monkeypatch.setattr(scraped_files, "BASES_DIR", str(tmp_path))
    df = pd.DataFrame({"Group": ["@omni", "@c123"], "Message ID": [1, 2]})
    if window:
        df.attrs["scrape_window"] = {"date_min": window[0], "date_max": window[1]}
    path = tmp_path / "Omni_posts_01.01.2024-31.12.2024.parquet"
    df.to_parquet(path)
    return str(path)


def test_ask_file_number_path_or_default(monkeypatch):
    from functions.base.base import BaseFunction

    files = [("a.parquet", "A"), ("b.parquet", "B")]
    for answer, expected in (("2", "b.parquet"), ("my/file.txt", "my/file.txt"), ("", "dflt.txt")):
        monkeypatch.setattr(scraper.Prompt, "ask", lambda *a, _x=answer, **k: _x)
        assert BaseFunction.ask_file("file", files, default="dflt.txt") == expected


def test_verify_func_prefills_channel_and_window(tmp_path, monkeypatch):
    path = _scraped(tmp_path, monkeypatch, window=("2024-01-01", "2024-12-31"))
    captured = {}
    monkeypatch.setattr(scraper, "pick_session", lambda storage, personal=None: _PICKED)
    monkeypatch.setattr(scraper, "verify_run", lambda creds, params: captured.update(params=params))

    defaults = []

    def ask(label, *a, default=None, **k):
        defaults.append(default)
        return {0: "1", 4: ""}.get(len(defaults) - 1, default)  # pick #1, accept every default

    monkeypatch.setattr(scraper.Prompt, "ask", ask)
    monkeypatch.setattr(scraper.TelethonFunction, "ask_int", staticmethod(lambda *a, **k: 0))
    scraper.VerifyFunc(FakeStorage(), FakeSettings()).execute()

    p = captured["params"]
    assert (p.input, p.channel) == (path, "@omni")
    assert (p.date_min.date().isoformat(), p.date_max.date().isoformat()) == ("2024-01-01", "2024-12-31")


def test_analysis_links_defaults_its_output_next_to_the_input(tmp_path, monkeypatch):
    path = _scraped(tmp_path, monkeypatch)
    captured = {}
    monkeypatch.setattr(analysis, "links", lambda inp, out: captured.update(args=(inp, out)))
    answers = iter(["1", "1"])  # tool 1 = links; file #1; then accept the default output
    monkeypatch.setattr(scraper_analysis.Prompt, "ask",
                        lambda *a, default=None, **k: next(answers, default))

    scraper_analysis.ScraperAnalysisFunc(FakeStorage(), FakeSettings()).execute()
    assert captured["args"] == (path, str(tmp_path / "Omni_links_01.01.2024-31.12.2024"))


def _checkpoint_with_account(tmp_path, account):
    import json

    ckpt = tmp_path / "Test_partial" / "checkpoint"
    ckpt.mkdir(parents=True)
    (ckpt / "resume.json").write_text(json.dumps({
        "name": "Test", "channels": ["@a"], "t_index": 7, "account": account,
        "date_min": "2024-01-01T00:00:00+00:00", "date_max": "2024-01-31T23:59:59+00:00",
        "channel_index": 0, "last_id": 5}))


def test_scrape_func_continues_on_the_checkpoint_s_own_account(monkeypatch, tmp_path):
    _checkpoint_with_account(tmp_path, "sessions/acc.jsession")
    storage = FakeStorage([_ACCOUNT])
    storage.get_session_path = lambda client: "sessions/acc.jsession"
    storage.jsessions_paths = {}
    captured = {}
    monkeypatch.setattr(scraper, "pick_session", lambda *a: (_ for _ in ()).throw(AssertionError("asked")))
    monkeypatch.setattr(scraper, "scrape_run", lambda creds, params: captured.update(creds=creds, params=params))
    _answers(monkeypatch, scraper, texts=["Test", str(tmp_path)], bools=[True])

    scraper.ScrapeFunc(storage, FakeSettings()).execute()
    assert captured["params"].account == "sessions/acc.jsession"
    assert captured["creds"].session_string == "SESSION"  # that account's own key


def test_scrape_func_won_t_continue_on_another_account(monkeypatch, tmp_path):
    _checkpoint_with_account(tmp_path, "personal_sessions/gone.jsession")
    printed, ran = [], []
    monkeypatch.setattr(scraper.console, "print", lambda *a, **k: printed.append(str(a[0])))
    monkeypatch.setattr(scraper, "scrape_run", lambda *a: ran.append(1))
    _answers(monkeypatch, scraper, texts=["Test", str(tmp_path)], bools=[True])

    scraper.ScrapeFunc(FakeStorage([]), FakeSettings()).execute()
    assert ran == [] and any("не найден" in p for p in printed)


def test_personal_accounts_are_listed_first_and_marked(tmp_path, monkeypatch):
    from modules import scraper_creds

    worker, me = object(), object()
    workers = FakeStorage([worker])
    workers.get_session_path, workers.jsessions_paths = lambda c: "sessions/w.jsession", {}
    personal = FakeStorage([me])
    personal.get_session_path, personal.jsessions_paths = lambda c: "personal_sessions/me.jsession", {}

    accounts = scraper_creds.scrape_accounts(workers, personal)
    assert [(a.path, a.personal) for a in accounts] == [
        ("personal_sessions/me.jsession", True), ("sessions/w.jsession", False)]
    assert scraper_creds.find_account(accounts, "sessions/w.jsession").client is worker
    assert scraper_creds.personal_storage(1, "h") is None  # no personal_sessions/ (isolated in tests)


def test_members_func_notes_a_personal_account(monkeypatch, tmp_path):
    from modules.scraper_creds import ScrapeAccount

    printed = []
    monkeypatch.setattr(scraper, "pick_session",
                        lambda *a: ScrapeAccount("personal_sessions/me.jsession", _ACCOUNT, "Me", True))
    monkeypatch.setattr(scraper.console, "print", lambda *a, **k: printed.append(str(a[0])))
    monkeypatch.setattr(scraper, "members_run", lambda creds, params: (tmp_path / "x.parquet", []))
    _answers(monkeypatch, scraper, texts=["@grp", "Grp", str(tmp_path)])

    scraper.MembersFunc(FakeStorage(), FakeSettings()).execute()
    assert any("только по " in p for p in printed)


def test_members_func_reports_a_logged_out_account(monkeypatch, tmp_path):
    from modules.scraper_creds import ScrapeAccount

    printed = []
    monkeypatch.setattr(scraper, "pick_session",
                        lambda *a: ScrapeAccount("sessions/w.jsession", _ACCOUNT, "W", False))
    monkeypatch.setattr(scraper.console, "print", lambda *a, **k: printed.append(str(a[0])))

    def dead(creds, params):
        raise SystemExit("the worker's session is no longer authorized - re-add the account")

    monkeypatch.setattr(scraper, "members_run", dead)
    _answers(monkeypatch, scraper, texts=["@grp", "Grp", str(tmp_path)])

    scraper.MembersFunc(FakeStorage(), FakeSettings()).execute()  # no SystemExit out of the menu
    assert any("no longer authorized" in p for p in printed)


def test_declining_to_continue_warns_the_saved_posts_go(monkeypatch, tmp_path):
    import json

    ckpt = tmp_path / "Test_partial" / "checkpoint"
    ckpt.mkdir(parents=True)
    (ckpt / "resume.json").write_text(json.dumps({
        "name": "Test", "channels": ["@a"], "t_index": 7,
        "date_min": "2024-01-01T00:00:00+00:00", "date_max": "2024-01-31T23:59:59+00:00"}))
    asked = []
    monkeypatch.setattr(scraper, "pick_session", lambda storage, personal=None: None)
    _answers(monkeypatch, scraper, texts=["Test", str(tmp_path)])  # then pick_session gives up
    monkeypatch.setattr(scraper.Confirm, "ask", lambda text, **k: asked.append(text) or False)

    scraper.ScrapeFunc(FakeStorage(), FakeSettings()).execute()

    # a fresh scrape under the same name clears the checkpoint: as the bot's button says
    assert "удалится" in asked[0]
