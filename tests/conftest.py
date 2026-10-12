import pytest


@pytest.fixture(autouse=True)
def _isolated_contacts_ledger(monkeypatch, tmp_path):
    """No test may write the real stats/contacts.json."""
    from modules import contacts_ledger

    monkeypatch.setattr(contacts_ledger, "LEDGER_PATH", str(tmp_path / "contacts.json"))


@pytest.fixture(autouse=True)
def _isolated_restricted_workers(monkeypatch, tmp_path):
    """No test may read or write the real stats/restricted.json, spambot_status.json or released.json."""
    from modules import restricted_workers

    monkeypatch.setattr(restricted_workers, "RESTRICTED_PATH", str(tmp_path / "restricted.json"))
    monkeypatch.setattr(restricted_workers, "STATUS_PATH", str(tmp_path / "spambot_status.json"))
    monkeypatch.setattr(restricted_workers, "RELEASED_PATH", str(tmp_path / "released.json"))


@pytest.fixture(autouse=True)
def _isolated_personal_accounts(monkeypatch, tmp_path):
    """No test may load the real personal_sessions/ (the user's own accounts)."""
    from modules import scraper_creds

    monkeypatch.setattr(scraper_creds, "PERSONAL_DIR", str(tmp_path / "personal_sessions"))


@pytest.fixture(autouse=True)
def _isolated_projects(monkeypatch, tmp_path):
    """No test may read or write the real stats/projects.json."""
    from modules import projects

    monkeypatch.setattr(projects, "PATH", str(tmp_path / "projects.json"))


@pytest.fixture(autouse=True)
def _isolated_bot_scrape(monkeypatch, tmp_path):
    """No test may write the real stats/bot_scrape.json; the router's scrape state starts clean."""
    from bot.routers import scraping
    from modules import scraped_files

    monkeypatch.setattr(scraped_files, "SCRAPE_MARKER", str(tmp_path / "bot_scrape.json"))
    monkeypatch.setattr(scraping, "_job_stop", None)
    monkeypatch.setattr(scraping, "_user_stopped", False)
    monkeypatch.setattr(scraping, "_shutting_down", False)


REAL_DO_ANALYSIS = None


@pytest.fixture(autouse=True)
def _analysis_in_process(monkeypatch):
    """The bot runs an analysis tool in a child process, which wouldn't see a test's patches
    of scraper.analysis: the flow tests run it here. The child process itself is tested
    with REAL_DO_ANALYSIS."""
    global REAL_DO_ANALYSIS
    from bot.routers import scraping as router
    from bot.services import scraping

    REAL_DO_ANALYSIS = REAL_DO_ANALYSIS or scraping.do_analysis

    async def in_process(tool, path, data):
        return router.run_tool(tool, path, data)

    monkeypatch.setattr(scraping, "do_analysis", in_process)


REAL_CHECK_WORKERS = None


@pytest.fixture(autouse=True)
def _no_spambot_preflight(monkeypatch):
    """Stub workers can't talk to @SpamBot: runs only apply the saved exclusions.
    Tests of the preflight itself restore REAL_CHECK_WORKERS."""
    global REAL_CHECK_WORKERS
    from functions.base.base import BaseFunction

    REAL_CHECK_WORKERS = REAL_CHECK_WORKERS or BaseFunction.check_workers

    async def apply_saved(self, report):
        if dropped := self.drop_restricted():
            await report(f"Пропущено бессрочно ограниченных воркеров: {dropped}")

    monkeypatch.setattr(BaseFunction, "check_workers", apply_saved)


REAL_WORKER_GONE = None


@pytest.fixture(autouse=True)
def _no_spambot_mid_run(monkeypatch):
    """Stub workers can't talk to @SpamBot: a worker stopped mid-run is never 'gone'.
    Tests of it restore REAL_WORKER_GONE."""
    global REAL_WORKER_GONE
    from functions.base.base import BaseFunction

    REAL_WORKER_GONE = REAL_WORKER_GONE or BaseFunction.worker_gone

    async def never(self, session, report):
        return False

    monkeypatch.setattr(BaseFunction, "worker_gone", never)


@pytest.fixture(autouse=True)
def _isolated_profile_done(monkeypatch, tmp_path):
    """No test may read or write the real stats/profile_done.json."""
    from modules import profile_done

    monkeypatch.setattr(profile_done, "PATH", str(tmp_path / "profile_done.json"))


REAL_BOT_LOOKUP = REAL_CLI_LOOKUP = None


@pytest.fixture(autouse=True)
def _no_chat_lookup(monkeypatch):
    """Stub accounts can't resolve chats: the flows go on with the input as typed, as when a
    lookup can't be made. Tests of the lookup restore REAL_BOT_LOOKUP / REAL_CLI_LOOKUP."""
    global REAL_BOT_LOOKUP, REAL_CLI_LOOKUP
    from bot.routers import scraping as router
    from functions import scraper

    REAL_BOT_LOOKUP = REAL_BOT_LOOKUP or router._lookup
    REAL_CLI_LOOKUP = REAL_CLI_LOOKUP or scraper._lookup

    async def no_lookup(message, pool, personal, path, chats):
        return None

    monkeypatch.setattr(router, "_lookup", no_lookup)
    monkeypatch.setattr(scraper, "_lookup", lambda account, chats: None)
