"""The accounts a scrape runs on (modules/scraper_creds.py): the session scripts' picker, personal ones first."""


import pytest


class FakeStorage:
    def __init__(self, sessions=()):
        self.sessions = list(sessions)


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



def test_pick_session_takes_the_only_account_at_once(monkeypatch):
    """As the bot's: with one account to scrape on, it is taken (and said), nothing asked."""
    from modules import scraper_creds

    client = object()
    storage = FakeStorage([client])
    storage.get_session_path = lambda client: "sessions/acc.jsession"
    storage.jsessions_paths = {}
    monkeypatch.setattr(scraper_creds.Prompt, "ask", lambda *a, **k: pytest.fail("nothing is asked"))
    monkeypatch.setattr(scraper_creds.console, "print", lambda *a, **k: None)

    assert scraper_creds.pick_session(storage).client is client
