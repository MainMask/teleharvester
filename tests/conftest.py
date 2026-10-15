import pytest

HOME = None  # the test's project folder (projects.home): see home()


def home() -> str:
    """The project folder of the test's fake storages: their mailing stats, contacts ledger, proxies."""
    return HOME


@pytest.fixture(autouse=True)
def _project_home(tmp_path):
    global HOME
    HOME = str(tmp_path / "home")


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
def _isolated_broadcast_temp(monkeypatch, tmp_path):
    """No test may create the real tmp/broadcast/ (as root it would be left root-owned)."""
    from bot.services import capture

    monkeypatch.setattr(capture, "BROADCAST_TMP_DIR", str(tmp_path / "broadcast"))


@pytest.fixture(autouse=True)
def _isolated_projects(monkeypatch, tmp_path):
    """No test may read or write the real stats/projects.json, projects/ (the projects' workers, stats,
    proxies), the free workers in sessions/ or the shared stats a migration copies."""
    from modules import projects

    monkeypatch.setattr(projects, "PATH", str(tmp_path / "projects.json"))
    monkeypatch.setattr(projects, "PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setattr(projects, "FREE_DIR", str(tmp_path / "free_sessions"))
    monkeypatch.setattr(projects, "LEGACY_STATS", {"pm_mailing.json": str(tmp_path / "legacy_pm_mailing.json"),
                                                   "contacts.json": str(tmp_path / "legacy_contacts.json")})


@pytest.fixture(autouse=True)
def _isolated_bot_scrape(monkeypatch, tmp_path):
    """No test may write the real stats/bot_scrape.json; the router's scrape state starts clean."""
    from bot.routers.scraping import common
    from modules import scraped_files

    monkeypatch.setattr(scraped_files, "SCRAPE_MARKER", str(tmp_path / "bot_scrape.json"))
    monkeypatch.setattr(common, "shutting_down", False)


REAL_DO_ANALYSIS = None


@pytest.fixture(autouse=True)
def _analysis_in_process(monkeypatch):
    """The bot runs an analysis tool in a child process, which wouldn't see a test's patches
    of scraper.analysis: the flow tests run it here. The child process itself is tested
    with REAL_DO_ANALYSIS."""
    global REAL_DO_ANALYSIS
    from bot.services import scraping
    from modules import analysis_tools

    REAL_DO_ANALYSIS = REAL_DO_ANALYSIS or scraping.do_analysis

    async def in_process(tool, path, data):
        return analysis_tools.run(tool, path, data)

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
    from bot.routers.scraping import common
    from functions import scraper

    REAL_BOT_LOOKUP = REAL_BOT_LOOKUP or common.lookup
    REAL_CLI_LOOKUP = REAL_CLI_LOOKUP or scraper._lookup

    async def no_lookup(message, pool, personal, path, chats):
        return None

    monkeypatch.setattr(common, "lookup", no_lookup)
    monkeypatch.setattr(scraper, "_lookup", lambda account, chats: None)


class Workspaces:
    """bot/services/workspaces.Workspaces holding one workspace — of `pool` and these slots —, whichever
    project is opened in it (the bot's routers take the project's from it)."""

    def __init__(self, pool=None, scrapes=None, manager=None, analyses=None):
        import types

        from bot.services.delegation import WorkerPool
        from bot.services.jobs import JobManager
        from bot.services.workspaces import Workspace

        pool = pool or WorkerPool(types.SimpleNamespace(sessions=[], jsessions_paths={}, home=home(),
                                                        get_session_path=lambda client: None))
        self.workspace = Workspace(1, pool, {}, manager or JobManager(), scrapes or JobManager(),
                                   analyses or JobManager())
        self.current, self.removed = {}, []

    def open(self, chat_id, project):
        self.workspace.project_id = self.current[chat_id] = project.id
        return self.workspace

    def get(self, project_id):
        return self.workspace if project_id == self.workspace.project_id else None

    def remove(self, project_id):
        self.removed.append(project_id)

    def all(self):
        return [self.workspace]

    def of_chat(self, chat_id):
        return self.workspace if chat_id in self.current else None

    def slot(self, key):
        return next((slot for slot in self.workspace.slots if slot.key == key), None)
