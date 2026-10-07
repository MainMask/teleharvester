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
def _isolated_bot_scrape(monkeypatch, tmp_path):
    """No test may write the real stats/bot_scrape.json; the router's scrape state starts clean."""
    from bot.routers import scraping

    monkeypatch.setattr(scraping, "MARKER_PATH", str(tmp_path / "bot_scrape.json"))
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
