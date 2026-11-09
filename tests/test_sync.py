"""Bot ↔ CLI parity: the delay setting, names/usernames from a list, scrape options in the
bot; the accounts list and proxies in the CLI."""

import asyncio
import datetime
import conftest
import functools
import io
import os
import types

import pytest
import toml

from bot.callbacks import ChoiceCB, FunctionCB, MenuAction, MenuCB
from bot.routers import menu
from bot.services.delegation import WorkerPool


def ns(**kw):
    return types.SimpleNamespace(**kw)


class _State:
    def __init__(self, data=None):
        self.data, self.state = dict(data or {}), None

    async def set_state(self, state):
        self.state = state

    async def get_state(self):
        return getattr(self.state, "state", self.state)  # aiogram's: the state's name

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_data(self):
        return dict(self.data)

    async def clear(self):
        self.data, self.state = {}, None


class _Sent:
    """A message the bot sent: what became of it."""

    def __init__(self, text):
        self.text, self.edited, self.deleted = text, [], False

    async def edit_text(self, text, **kwargs):
        self.edited.append(text)

    async def delete(self):
        self.deleted = True


class _Msg:
    def __init__(self, text=None, document=None, content=b""):
        self.text, self.document, self.answers, self.sent = text, document, [], []
        self.chat = ns(id=1)
        self.date, self.edit_date = datetime.datetime.now(datetime.timezone.utc), None  # as Telegram's

        async def get_file(file_id, request_timeout=None):  # the file Telegram gives (_common.download)
            return ns(file_path="documents/file")

        async def download_file(file_path, timeout=30):
            return io.BytesIO(content)

        self.bot = ns(get_file=get_file, download_file=download_file)

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs.get("reply_markup")))
        self.sent.append(_Sent(text))
        return self.sent[-1]

    async def edit_text(self, text, **kwargs):  # in place: what is shown now, as an answer
        self.answers.append((text, kwargs.get("reply_markup")))

    async def edit_reply_markup(self, reply_markup=None):
        pass


class _Callback:
    def __init__(self):
        self.message, self.alerts = _Msg(), []

    async def answer(self, text=None, **k):
        if text:
            self.alerts.append(text)


def _buttons(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def _launches(monkeypatch=None):
    """A JobManager stand-in: records the kwargs each launched run() got."""
    runs = []

    class _Manager:
        active = False

        async def run(self, bot, chat_id, pool, instance, bot_function, job, *labels, only=None):
            async def run(report, **kwargs):
                runs.append(kwargs)

            await job(ns(run=run), None)

    return _Manager(), runs


# --- 1. the delay -------------------------------------------------------------------------

def _settings(tmp_path, monkeypatch):
    from modules.settings import Settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", "h")
    (tmp_path / "config.toml").write_text(toml.dumps({
        "broadcast": {"messages": ["hi"], "messages_count": 0, "trigger": "go", "delay": [5, 10]},
        "limits": {"per_account_daily": 99, "account_pause": [1, 2]},
    }))
    return Settings()


def test_set_delay_keeps_every_other_setting(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    settings.set_delay([2, 4])

    config = toml.load(tmp_path / "config.toml")
    assert config["broadcast"]["delay"] == [2, 4] and settings.delay == [2, 4]
    assert config["limits"] == {"per_account_daily": 99, "account_pause": [1, 2]}
    assert config["broadcast"]["messages"] == ["hi"]


def test_set_delay_keeps_the_files_mode(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    os.chmod(tmp_path / "config.toml", 0o600)
    settings.set_delay([2, 4])
    assert (tmp_path / "config.toml").stat().st_mode & 0o777 == 0o600


def test_every_config_write_backs_up_the_previous_file(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    os.chmod(tmp_path / "config.toml", 0o600)  # it holds the bot's admin whitelist
    before = (tmp_path / "config.toml").read_text()
    settings.set_delay([2, 4])

    backup = tmp_path / "config.toml.bak"
    assert backup.read_text() == before and backup.stat().st_mode & 0o777 == 0o600
    settings.set_trigger("start")
    assert toml.loads(backup.read_text())["broadcast"]["delay"] == [2, 4]  # the write before this one


def test_set_delay_keeps_the_files_comments(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    (tmp_path / "config.toml").write_text(
        "[bot]\n"
        "# admins = Telegram user IDs allowed to control the bot\n"
        "admins = [1]\n\n"
        "[broadcast]\n"
        "messages = []\n"
        "delay = [5, 10]  # seconds\n"
        "messages_count = 0   # 0 = unlimited\n"
        'trigger = ""\n\n'
        "[limits]\n"
        "delay = [7, 7]\n"  # another section's key of the same name stays
    )
    settings.set_delay([2, 4])

    text = (tmp_path / "config.toml").read_text()
    assert "delay = [2, 4]  # seconds\n" in text
    assert "# admins = Telegram user IDs" in text and "# 0 = unlimited" in text
    assert toml.loads(text)["limits"]["delay"] == [7, 7]
    assert not (tmp_path / "config.toml.tmp").exists()


def test_set_profile_pause_writes_limits_and_keeps_others(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    settings.set_profile_pause([90, 240])

    config = toml.load(tmp_path / "config.toml")
    assert config["limits"]["profile_pause"] == [90, 240] and settings.profile_pause == [90, 240]
    assert config["limits"]["per_account_daily"] == 99  # other limits untouched
    assert config["broadcast"]["delay"] == [5, 10]      # other sections untouched


def test_set_profile_pause_rewrites_only_its_line(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    (tmp_path / "config.toml").write_text(
        "[broadcast]\n"
        "delay = [5, 10]  # seconds\n"
        "messages = []\n"
        "messages_count = 0\n"
        'trigger = ""\n\n'
        "[limits]\n"
        "profile_pause = [60, 180]  # keep me\n"
    )
    settings.set_profile_pause([90, 240])

    text = (tmp_path / "config.toml").read_text()
    assert "profile_pause = [90, 240]  # keep me\n" in text
    assert "delay = [5, 10]  # seconds\n" in text  # the [broadcast] delay line is left alone
    assert not (tmp_path / "config.toml.tmp").exists()


def test_set_trigger_and_messages_count_rewrite_their_lines(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    (tmp_path / "config.toml").write_text(
        "[broadcast]\n"
        "messages = []\n"
        "delay = [5, 10]\n"
        "messages_count = 0   # 0 = unlimited\n"
        'trigger = ""\n'
    )
    settings.set_trigger('старт "go"')
    settings.set_messages_count(3)

    text = (tmp_path / "config.toml").read_text()
    assert "messages_count = 3   # 0 = unlimited\n" in text
    assert toml.loads(text)["broadcast"]["trigger"] == 'старт "go"'
    assert (settings.trigger, settings.messages_count) == ('старт "go"', 3)


def test_bot_delay_reports_a_broken_config(tmp_path, monkeypatch):
    from bot.routers import settings as screen

    settings = _settings(tmp_path, monkeypatch)
    (tmp_path / "config.toml").write_text("[broadcast\ndelay = [5, 10]\n")  # broken by hand meanwhile
    msg = _Msg(text="2-4")
    asyncio.run(screen.setting_typed(msg, _State({"setting": "delay"}), settings))
    assert "Не удалось сохранить config.toml" in msg.answers[0][0] and settings.delay == [5, 10]


def test_bot_delay_button(tmp_path, monkeypatch):
    from bot.routers import settings as screen

    settings = _settings(tmp_path, monkeypatch)
    state, callback = _State(), _Callback()
    asyncio.run(screen.setting_start(callback, MenuCB(action=MenuAction.DELAY), state, settings))
    assert "5–10 с" in callback.message.answers[0][0]

    for bad in ("abc", "1-2-3", "", "-5"):
        msg = _Msg(text=bad)
        asyncio.run(screen.setting_typed(msg, state, settings))
        assert "Пришлите ещё раз" in msg.answers[0][0]
    assert settings.delay == [5, 10]

    msg = _Msg(text="7")
    asyncio.run(screen.setting_typed(msg, state, settings))
    assert settings.delay == [7] and toml.load(tmp_path / "config.toml")["broadcast"]["delay"] == [7]


def test_bot_profile_pause_rejects_bad_input_and_saves_a_range(tmp_path, monkeypatch):
    from bot.routers import settings as screen

    settings = _settings(tmp_path, monkeypatch)
    state = _State({"setting": "ppause"})
    for bad in ("abc", "1-2-3", "", "-5"):
        msg = _Msg(text=bad)
        asyncio.run(screen.setting_typed(msg, state, settings))
        assert "Пришлите ещё раз" in msg.answers[0][0]

    msg = _Msg(text="90 - 240")
    asyncio.run(screen.setting_typed(msg, state, settings))
    assert settings.profile_pause == [90, 240]
    assert toml.load(tmp_path / "config.toml")["limits"]["profile_pause"] == [90, 240]

# --- 2. names / usernames from a list ------------------------------------------------------

def test_names_from_a_sent_txt(monkeypatch):
    from bot.routers import profile

    manager, runs = _launches()
    msg = _Msg(document=ns(file_id="f", file_size=20), content="Иван Петров\n\nАнна\n".encode())
    asyncio.run(profile.text_list(msg, _State({"pick_key": "name"}), ns(), {"ChangeNameFunc": object}, manager))
    assert runs == [{"names": ["Иван Петров", "Анна"]}]


def test_names_button_from_the_project_s_region_list(tmp_path, monkeypatch):
    from bot.routers import profile
    from modules import content

    os.makedirs(content.path("ru", ""))
    with open(content.path("ru", "names.txt"), "w", encoding="utf-8") as fileobj:
        fileobj.write("A\nB\nC\n")
    state = _State()
    asyncio.run(profile.picker_start(_Callback(), FunctionCB(key="name"), state, _pool()))
    _pick("all", state, _pool())
    callback = _pick("go", state, _pool())
    assert _buttons(callback.message.answers[0][1]) == [f"📄 Из {content.path('ru', 'names.txt')} (3)", "✖️ Отмена"]

    manager, runs = _launches()
    asyncio.run(profile.text_from_file(_Callback(), _State({"pick_key": "name"}), _pool(), {"ChangeNameFunc": object},
                                       manager))
    assert runs == [{"names": ["A", "B", "C"]}]


def test_typed_name_works_as_before():
    from bot.routers import profile

    manager, runs = _launches()
    asyncio.run(profile.text_typed(_Msg(text="Иван Петров"), _State({"pick_key": "name"}), ns(),
                                   {"ChangeNameFunc": object}, manager))
    assert runs == [{"first_name": "Иван", "last_name": "Петров"}]


def test_usernames_from_a_sent_txt():
    from bot.routers import profile

    manager, runs = _launches()
    msg = _Msg(document=ns(file_id="f", file_size=20), content=b"@one\ntwo\n")
    asyncio.run(profile.text_list(msg, _State({"pick_key": "username"}), ns(), {"ChangeUsernameFunc": object}, manager))
    assert runs == [{"usernames": ["one", "two"]}]


def test_empty_list_is_asked_again():
    from bot.routers import profile

    manager, runs = _launches()
    msg = _Msg(document=ns(file_id="f", file_size=2), content=b"\n\n")
    asyncio.run(profile.text_list(msg, _State({"pick_key": "username"}), ns(), {"ChangeUsernameFunc": object}, manager))
    assert runs == [] and "нет ни одной строки" in msg.answers[0][0]


# --- 3. scrape: account, its own slot, options, stop, continuing ----------------------------

_ACC = "sessions/w.jsession"
_SCRAPE = {"channels": "@a", "name": "n", "out_dir": "out", "date_min": "01.01.2024", "account": _ACC}


def _pool(*paths):
    """A worker pool whose storage holds these session paths (one by default)."""
    paths = paths or (_ACC,)
    clients = [object() for _ in paths]
    by_client = dict(zip(map(id, clients), paths))
    return WorkerPool(ns(sessions=clients, jsessions_paths={}, home=conftest.home(),
                         directory=f"{conftest.home()}/sessions", get_session_path=lambda c: by_client[id(c)]))


class _SBot:
    """The bot a scrape reports through (it can run with no message, after a restart)."""

    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self.sent.append((text, reply_markup))

        async def edit_reply_markup(reply_markup=None):
            pass
        return ns(edit_reply_markup=edit_reply_markup)


class _SMsg(_Msg):
    def __init__(self, text=None, bot=None):
        super().__init__(text=text)
        self.bot, self.edited, self.edits = bot or _SBot(), [], []

    async def edit_reply_markup(self, reply_markup=None):
        self.edited.append(reply_markup)

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs.get("reply_markup")))
        self.edit_date = datetime.datetime.now(datetime.timezone.utc)


class _SCallback:
    def __init__(self, bot=None):
        self.bot = bot or _SBot()
        self.message = _SMsg(bot=self.bot)

    async def answer(self, *a, **k):
        pass


def _scrapes(monkeypatch, behaviour=None):
    """Record each do_scrape's params; behaviour(params) may raise to end the run."""
    from bot.routers import scraping as router
    from bot.services import scraping

    calls = []

    async def do_scrape(creds, params):
        calls.append(params)
        if behaviour is not None:
            result = behaviour(params)
            if asyncio.iscoroutine(result):
                await result

    monkeypatch.setattr(scraping, "do_scrape", do_scrape)
    monkeypatch.setattr(scraping, "dir_snapshot", lambda path: {})
    monkeypatch.setattr(scraping, "new_files", lambda path, before, prefixes: [])
    monkeypatch.setattr(router.scrape, "build_credentials", lambda client: None)
    return calls


def _sent(bot):
    return [text for text, _ in bot.sent]


def test_scrape_asks_before_starting_then_runs_in_its_own_slot(monkeypatch):
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager

    scrapes, manager = JobManager(), JobManager()
    seen = []
    calls = _scrapes(monkeypatch, lambda params: seen.append((scrapes.active, manager.active)))
    state = _State(_SCRAPE)
    msg = _Msg(text="31.01.2024")
    asyncio.run(router.scrape.scrape_run(msg, state))
    assert calls == [] and _buttons(msg.answers[0][1]) == ["▶️ Запустить", "🎛 Дополнительно", "✖️ Отмена"]

    go = _SCallback()
    asyncio.run(router.scrape.scrape_go(go, ChoiceCB(scope="scrape_go", value="run"), state, _pool(), None, scrapes))
    [params] = calls
    assert params.keyword == "" and params.with_reactors and params.with_comments and params.resume is False
    assert params.account == _ACC
    assert seen == [(True, False)]  # the scraper's slot is taken, the bot's task slot stays free
    assert not scrapes.active and any("▶️ Скрап — запущено на аккаунте" in t for t in _sent(go.bot))


def test_scrape_options_toggle_in_place_and_reach_the_run(monkeypatch):
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager
    from bot.states import Scrape

    calls = _scrapes(monkeypatch)
    state = _State({**_SCRAPE, "date_max": "31.01.2024"})
    state.state = Scrape.confirm

    more = _SCallback()
    asyncio.run(router.scrape.scrape_go(more, ChoiceCB(scope="scrape_go", value="more"), state, _pool(), None,
                                 JobManager()))
    assert _buttons(more.message.edits[0][1]) == [  # in the summary's place
        "🗨 Комментарии ✅", "❤️ Реакции (медленно) ✅", "👥 База участников ✅",
        "🔎 Фильтр по слову: —", "🔢 Максимум постов: все", "▶️ Запустить", "✖️ Отмена"]

    toggle = _SCallback()
    asyncio.run(router.scrape.scrape_option(toggle, ChoiceCB(scope="scrape_opt", value="reactors"), state,
                                     _pool(), None, JobManager()))
    assert _buttons(toggle.message.edited[0])[1] == "❤️ Реакции (медленно) ❌"  # edited, not resent

    ask = _SCallback()
    asyncio.run(router.scrape.scrape_option(ask, ChoiceCB(scope="scrape_opt", value="keyword"), state,
                                     _pool(), None, JobManager()))
    assert state.state == Scrape.keyword
    asyncio.run(router.scrape.scrape_keyword(_Msg(text="крипта"), state))
    asyncio.run(router.scrape.scrape_option(_SCallback(), ChoiceCB(scope="scrape_opt", value="max"), state,
                                     _pool(), None, JobManager()))
    bad = _Msg(text="abc")
    asyncio.run(router.scrape.scrape_max(bad, state))
    assert state.state == Scrape.max_messages and "число" in bad.answers[0][0]
    asyncio.run(router.scrape.scrape_max(_Msg(text="500"), state))
    assert state.state == Scrape.confirm

    asyncio.run(router.scrape.scrape_option(_SCallback(), ChoiceCB(scope="scrape_opt", value="run"), state,
                                     _pool(), None, JobManager()))
    [params] = calls
    assert (params.keyword, params.with_reactors, params.max_messages) == ("крипта", False, 500)


def test_an_interrupted_scrape_keeps_its_marker_and_says_how_to_continue(monkeypatch):
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager

    def interrupted(params):
        raise SystemExit(1)

    _scrapes(monkeypatch, interrupted)
    bot = _SBot()
    params = router.scrape._scrape_params({**_SCRAPE, "date_max": "31.01.2024"})
    asyncio.run(router.scrape._run_scrape(bot, 1, JobManager(), _pool(), None, params))
    assert any("нажмите «▶️ Скрап» — бот предложит продолжить" in t for t in _sent(bot))
    assert router.scrape._read_marker(conftest.home()) == {"out_dir": "out", "name": "n", "account": _ACC, "chat_id": 1}


def test_the_stop_button_stops_and_drops_the_marker(monkeypatch):
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager

    scrapes = JobManager()

    async def press_stop(params):
        # the operator's ⏹, mid-run
        await menu.stop_job(_SCallback(), ChoiceCB(scope="scrape_stop", value=str(scrapes.key)),
                               conftest.Workspaces(scrapes=scrapes))
        assert params.stop.is_set()
        raise SystemExit(1)  # the scrape checkpoints and exits

    _scrapes(monkeypatch, press_stop)
    bot = _SBot()
    params = router.scrape._scrape_params({**_SCRAPE, "date_max": "31.01.2024"})
    asyncio.run(router.scrape._run_scrape(bot, 1, scrapes, _pool(), None, params))
    assert any(t.startswith("⏹ Скрап остановлен") for t in _sent(bot))
    assert router.scrape._read_marker(conftest.home()) is None and not scrapes.active


def test_a_bot_shutdown_checkpoints_silently_and_keeps_the_marker(monkeypatch):
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager

    async def checkpoints_on_stop(params):
        while not params.stop.is_set():
            await asyncio.sleep(0.01)
        raise SystemExit(1)

    _scrapes(monkeypatch, checkpoints_on_stop)
    bot = _SBot()
    params = router.scrape._scrape_params({**_SCRAPE, "date_max": "31.01.2024"})

    scrapes = JobManager()

    async def scenario():
        run = asyncio.create_task(router.scrape._run_scrape(bot, 1, scrapes, _pool(), None, params))
        await asyncio.sleep(0.05)
        await router.scrape.stop_for_shutdown(conftest.Workspaces(scrapes=scrapes))
        # returns once the job is over: its last bot call came before aiogram closes the session
        assert not scrapes.active
        await run

    asyncio.run(scenario())
    assert not any("прерван" in t or "остановлен" in t for t in _sent(bot))  # the bot is going down
    assert router.scrape._read_marker(conftest.home()) is not None


def test_a_finished_scrape_drops_the_marker(monkeypatch):
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager

    _scrapes(monkeypatch)
    params = router.scrape._scrape_params({**_SCRAPE, "date_max": "31.01.2024"})
    asyncio.run(router.scrape._run_scrape(_SBot(), 1, JobManager(), _pool(), None, params))
    assert router.scrape._read_marker(conftest.home()) is None


def test_a_worker_in_a_bot_job_is_not_scraped_on(monkeypatch):
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager

    calls, bot, scrapes, pool = _scrapes(monkeypatch), _SBot(), JobManager(), _pool()
    pool.in_job = pool.workers  # a mailing runs on it
    params = router.scrape._scrape_params({**_SCRAPE, "date_max": "31.01.2024"})
    asyncio.run(router.scrape._run_scrape(bot, 1, scrapes, pool, None, params))
    assert calls == [] and _sent(bot) == [router.common.WORKER_BUSY]
    assert not scrapes.active and router.scrape._read_marker(conftest.home()) is None and pool.scraping is None


def test_a_scraping_worker_is_kept_out_of_bot_jobs_until_the_scrape_ends(monkeypatch):
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager

    pool, during = _pool(), []

    def check(params):
        during.append(pool.scraping.path)
        raise RuntimeError("process killed")  # it is released on an error too

    _scrapes(monkeypatch, check)
    bot = _SBot()
    params = router.scrape._scrape_params({**_SCRAPE, "date_max": "31.01.2024"})
    asyncio.run(router.scrape._run_scrape(bot, 1, JobManager(), pool, None, params))
    assert during == [_ACC] and pool.scraping is None
    assert any("без этого воркера" in t for t in _sent(bot))


def test_a_failed_marker_write_frees_the_slot_and_the_worker(monkeypatch):
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager

    def no_space(key, marker):
        raise OSError(28, "No space left on device")

    calls, bot, scrapes, pool = _scrapes(monkeypatch), _SBot(), JobManager(), _pool()
    monkeypatch.setattr(router.scrape, "_write_marker", no_space)
    params = router.scrape._scrape_params({**_SCRAPE, "date_max": "31.01.2024"})
    asyncio.run(router.scrape._run_scrape(bot, 1, scrapes, pool, None, params))
    assert calls == [] and any("No space left" in t for t in _sent(bot))
    assert not scrapes.active and pool.scraping is None


def test_a_gone_account_is_reported_and_nothing_runs(monkeypatch):
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager

    calls = _scrapes(monkeypatch)
    bot = _SBot()
    params = router.scrape._scrape_params({**_SCRAPE, "date_max": "31.01.2024", "account": "sessions/gone.jsession"})
    asyncio.run(router.scrape._run_scrape(bot, 1, JobManager(), _pool(), None, params))
    assert calls == [] and "не найден" in _sent(bot)[0]


# --- the account question: workers and personal accounts ------------------------------------

def _project_scrape(router, msg, state, pool=None, name="n"):
    """A project's ▶️ Скрап (the only way to start a scrape): the project's name and channels."""
    from bot.services.jobs import JobManager

    asyncio.run(router.scrape.scrape_project(msg, state, ns(name=name, channels=["@a", "@b"]),
                                      dict(pool=pool or _pool(), personal=None, scrapes=JobManager())))


def test_one_account_is_taken_without_asking(monkeypatch, tmp_path):
    from bot.routers import scraping as router
    from bot.states import Scrape

    monkeypatch.setattr(router.common, "DEFAULT_OUT", str(tmp_path))
    state = _State({})
    msg = _SMsg()  # no output folder is asked, the bases folder is the one
    _project_scrape(router, msg, state)
    assert state.data["account"] == _ACC and state.state == Scrape.date_min  # no «На каком аккаунте?»
    assert state.data["name"] == "n" and state.data["out_dir"] == str(tmp_path)
    assert not any(text.startswith("На каком аккаунте") for text, _ in msg.answers)


def test_a_personal_account_is_offered_marked_and_warned_about(tmp_path):
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager
    from bot.states import Members

    personal = ns(sessions=[object()], jsessions_paths={},
                  get_session_path=lambda c: "personal_sessions/me.jsession")
    state = _State()
    start = _SCallback()
    asyncio.run(router.members.members_project(start.message, state, ns(name="P"),
                                       dict(pool=_pool(), personal=personal, scrapes=JobManager())))
    # in the card's place, personal first
    assert _buttons(start.message.edits[0][1]) == ["👤 me.jsession", "🤖 w.jsession", "⬅️ 📥 Сбор"]

    pick = _SCallback()
    asyncio.run(router.common.account_pick(pick, ChoiceCB(scope="scr_acc", value="0"), state, _pool(), personal,
                                    JobManager()))
    assert state.data["account"] == "personal_sessions/me.jsession" and state.state == Members.chats
    warning = pick.message.answers[0][0]
    assert warning.startswith("⚠️ Это ваш личный аккаунт") and "только тем, у кого есть username" in warning


def test_many_accounts_to_scrape_on_are_paged():
    """Telegram takes at most 100 buttons under a message: the accounts are offered 20 a page."""
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager
    from bot.states import Members

    pool = _pool(*[f"sessions/w{i:02}.jsession" for i in range(25)])
    state = _State()
    start = _SCallback()
    asyncio.run(router.members.members_project(start.message, state, ns(name="P"),
                                       dict(pool=pool, personal=None, scrapes=JobManager())))
    assert _buttons(start.message.edits[0][1])[-4:] == ["◀️", "📄 стр. 1/2", "▶️", "⬅️ 📥 Сбор"]

    page = _SCallback()
    asyncio.run(router.common.account_pick(page, ChoiceCB(scope="scr_acc", value="pg.1"), state, pool, None,
                                           JobManager()))
    assert _buttons(page.message.edited[-1]) == ["🤖 w20.jsession", "🤖 w21.jsession", "🤖 w22.jsession",
                                                 "🤖 w23.jsession", "🤖 w24.jsession", "◀️", "📄 стр. 2/2", "▶️",
                                                 "⬅️ 📥 Сбор"]
    asyncio.run(router.common.account_pick(_SCallback(), ChoiceCB(scope="scr_acc", value="22"), state, pool, None,
                                    JobManager()))
    assert state.data["account"] == "sessions/w22.jsession" and state.state == Members.chats


def test_verify_on_a_personal_account_warns_only_about_limits():
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager

    personal = ns(sessions=[object()], jsessions_paths={},
                  get_session_path=lambda c: "personal_sessions/me.jsession")
    state = _State()
    asyncio.run(router.verify.verify_project(_SCallback().message, state, ns(name="P"),
                                      dict(pool=_pool(), personal=personal, scrapes=JobManager())))
    pick = _SCallback()
    asyncio.run(router.common.account_pick(pick, ChoiceCB(scope="scr_acc", value="0"), state, _pool(), personal,
                                    JobManager()))
    warning = pick.message.answers[0][0]
    assert warning.startswith("⚠️ Это ваш личный аккаунт") and "username" not in warning  # no base here


def test_a_personal_scrape_holds_its_account_too():
    """Its login codes (🔑 Код входа) must not connect a second client to the scraped key."""
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager

    pool, account = _pool(), ns(path="personal_sessions/me.jsession", label="me", personal=True)

    async def say(text):
        raise AssertionError(text)
    assert asyncio.run(router.common.take_slot(JobManager(), pool, account, "Скрап", say)) is not None
    assert pool.scraping is account


def test_no_scrape_starts_on_a_personal_account_whose_codes_are_read():
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager

    pool, account, said = _pool(), ns(path="personal_sessions/me.jsession", label="me", personal=True), []
    pool.borrowed[account.path] += 1

    async def say(text):
        said.append(text)
    scrapes = JobManager()
    assert asyncio.run(router.common.take_slot(scrapes, pool, account, "Скрап", say)) is None
    assert said == [router.common.PERSONAL_BUSY] and not scrapes.active and pool.scraping is None


def test_the_verify_stop_says_stopped(monkeypatch, tmp_path):
    from bot.routers import scraping as router
    from bot.services import scraping
    from bot.services.jobs import JobManager

    scrapes = JobManager()

    async def stopped(creds, params):
        await menu.stop_job(_SCallback(), ChoiceCB(scope="scrape_stop", value=str(scrapes.key)),
                               conftest.Workspaces(scrapes=scrapes))
        raise SystemExit(1)  # verify ends as interrupted

    monkeypatch.setattr(scraping, "do_verify", stopped)
    monkeypatch.setattr(router.verify, "build_credentials", lambda client: None)
    msg = _SMsg()
    asyncio.run(router.verify._run_verify(msg, scrapes, _pool(), ns(client=None, label="w", path=_ACC, personal=False),
                                   str(tmp_path / "x_posts.parquet"), "@a", "01.01.2024", "31.01.2024"))
    assert _buttons(msg.answers[0][1]) == ["📊 Прогресс", "⏹ Стоп"]
    assert msg.answers[-1][0] == "⏹ Верификация остановлена." and not scrapes.active


def test_a_stale_account_button_says_so():
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager

    pick = _Callback()
    asyncio.run(router.common.account_pick(pick, ChoiceCB(scope="scr_acc", value="0"), _State(), _pool(), None,
                                           JobManager()))
    assert pick.alerts == ["Кнопка устарела — откройте раздел заново."] and not pick.message.answers


# --- continuing an interrupted scrape from the bot ----------------------------------------

def _checkpoint(out_dir, name="n", account=_ACC, **extra):
    import json

    ckpt = out_dir / f"{name}_partial" / "checkpoint"
    ckpt.mkdir(parents=True)
    meta = {"name": name, "channels": ["@a", "@b"], "keyword": "", "t_index": 42,
            "date_min": "2024-01-01T00:00:00+00:00", "date_max": "2024-01-31T23:59:59+00:00",
            "with_reactors": False, "channel_index": 1, "last_id": 9}
    if account is not None:
        meta["account"] = account
    meta.update(extra)
    (ckpt / "resume.json").write_text(json.dumps(meta))


def test_scrape_offers_to_continue_an_interrupted_one_on_its_account(monkeypatch, tmp_path):
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager
    from bot.states import Scrape

    calls = _scrapes(monkeypatch)
    _checkpoint(tmp_path)
    monkeypatch.setattr(router.common, "DEFAULT_OUT", str(tmp_path))
    state = _State({})
    msg = _SMsg()
    _project_scrape(router, msg, state)

    text, markup = msg.edits[0]  # in 📥 Сбор's place
    assert state.state == Scrape.resume and "Уже собрано постов: 42" in text and "1. @a\n\n2. @b" in text
    assert _buttons(markup)[0] == "▶️ Продолжить"

    asyncio.run(router.scrape.scrape_resume(_SCallback(), ChoiceCB(scope="scrape_resume", value="continue"),
                                     state, _pool("sessions/other.jsession", _ACC), None, JobManager()))
    [params] = calls
    assert params.resume and params.channels == ["@a", "@b"] and params.with_reactors is False
    assert params.out_dir == tmp_path and params.name == "n" and params.account == _ACC


def test_an_old_checkpoint_without_an_account_asks_for_one(monkeypatch, tmp_path):
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager

    calls = _scrapes(monkeypatch)
    _checkpoint(tmp_path, account=None)
    state = _State({"name": "n", "out_dir": str(tmp_path)})
    asyncio.run(router.scrape.scrape_resume(_SCallback(), ChoiceCB(scope="scrape_resume", value="continue"),
                                     state, _pool(), None, JobManager()))
    [params] = calls  # one account: taken at once, then continued on it
    assert params.resume and params.account == _ACC


def test_scrape_start_fresh_keeps_the_project_s_channels(monkeypatch, tmp_path):
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager
    from bot.states import Scrape

    calls = _scrapes(monkeypatch)
    _checkpoint(tmp_path)
    state = _State({"name": "n", "out_dir": str(tmp_path), "channels": "@a, @b"})
    state.state = Scrape.resume
    fresh = _SCallback()
    asyncio.run(router.scrape.scrape_resume(fresh, ChoiceCB(scope="scrape_resume", value="fresh"),
                                     state, _pool(), None, JobManager()))
    assert calls == [] and state.state == Scrape.date_min  # the channels aren't asked: the project's
    assert state.data["channels"] == "@a, @b"


def test_a_new_scrape_drops_an_abandoned_ones_settings():
    from bot.routers import scraping as router

    state = _State({"keyword": "крипта", "max_messages": 50, "comments": False})
    _project_scrape(router, _SMsg(), state)
    assert not {"keyword", "max_messages", "comments"} & set(state.data)  # no hidden filter on the new run


def test_a_broken_marker_reads_as_none(monkeypatch, tmp_path):
    from bot.routers import scraping as router
    from modules import scraped_files

    monkeypatch.setattr(scraped_files, "SCRAPE_MARKER", str(tmp_path / "bot_scrape.json"))
    (tmp_path / "bot_scrape.json").write_text("{half-writ")
    assert router.scrape._read_marker(conftest.home()) is None


def test_a_restart_continues_the_marked_scrape(monkeypatch, tmp_path):
    from bot.routers import scraping as router

    calls = _scrapes(monkeypatch)
    _checkpoint(tmp_path)
    router.scrape._write_marker(conftest.home(), {"out_dir": str(tmp_path), "name": "n", "account": _ACC, "chat_id": 7})
    bot = _SBot()

    async def scenario():
        await router.scrape.resume_after_restart(bot, conftest.Workspaces(_pool()), None)
        await asyncio.sleep(0.05)  # it runs in the background

    asyncio.run(scenario())
    [params] = calls
    assert params.resume and params.account == _ACC and params.channels == ["@a", "@b"]
    assert _sent(bot)[0].startswith("🔄 Бот перезапущен")


def test_a_restart_without_a_checkpoint_drops_a_stale_marker(monkeypatch, tmp_path):
    from bot.routers import scraping as router

    calls = _scrapes(monkeypatch)
    router.scrape._write_marker(conftest.home(), {"out_dir": str(tmp_path), "name": "n", "account": _ACC, "chat_id": 7})
    asyncio.run(router.scrape.resume_after_restart(_SBot(), conftest.Workspaces(_pool()), None))
    assert calls == [] and router.scrape._read_marker(conftest.home()) is None


def test_a_restart_drops_the_marker_of_a_gone_account(monkeypatch, tmp_path):
    from bot.routers import scraping as router

    calls = _scrapes(monkeypatch)
    gone = "sessions/gone.jsession"
    _checkpoint(tmp_path, account=gone)
    router.scrape._write_marker(conftest.home(), {"out_dir": str(tmp_path), "name": "n", "account": gone, "chat_id": 7})
    bot = _SBot()

    async def scenario():
        await router.scrape.resume_after_restart(bot, conftest.Workspaces(_pool()), None)
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    assert calls == [] and router.scrape._read_marker(conftest.home()) is None  # no retry on the next restart
    assert any("не найден" in text for text in _sent(bot))


def test_a_restart_that_returns_early_still_counts_the_stall(monkeypatch, tmp_path):
    from bot.routers import scraping as router

    calls = _scrapes(monkeypatch)
    _checkpoint(tmp_path)  # t_index 42, as in the marker: no progress since
    router.scrape._write_marker(conftest.home(), {"out_dir": str(tmp_path), "name": "n", "account": _ACC, "chat_id": 7,
                          "t_index": 42, "stalls": 0})
    pool = _pool()
    pool.in_job = pool.workers  # its worker is busy: _run_scrape returns before its own marker

    async def scenario():
        await router.scrape.resume_after_restart(_SBot(), conftest.Workspaces(pool), None)
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    assert calls == [] and router.scrape._read_marker(conftest.home())["stalls"] == 1


# --- 4. the session scripts: tdata import ---------------------------------------------------------


def test_cli_tdata_import_adds_the_new_workers_without_a_restart(tmp_path, monkeypatch):
    from functions import sign_in as cli

    monkeypatch.chdir(tmp_path)
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions" / "old.jsession").write_text("{}")
    said = []
    monkeypatch.setattr(cli, "say", said.append)

    async def import_all(workers_dir, sessions_dir, proxies_path):
        assert (sessions_dir, proxies_path) == ("sessions", os.path.join("home", "proxies.txt"))
        (tmp_path / "sessions" / "new.jsession").write_text("{}")
        return 1

    monkeypatch.setattr(cli, "import_all", import_all)
    added = []

    class _Storage:
        sessions = []
        full_sessions = {os.path.join("sessions", "old.jsession"): "old"}
        directory, home = "sessions", "home"  # the project's

        def add_jsession(self, path):
            added.append(path)
            self.full_sessions[path] = "client"
            return object()

        def __len__(self):
            return len(self.full_sessions)

    async def no_sessions(root, storage):
        return [], []

    monkeypatch.setattr(cli.session_import, "import_all", no_sessions)
    asyncio.run(cli.AddByPhoneFunc(_Storage(), ns()).import_files())
    new = os.path.join("sessions", "new.jsession")
    assert added == [new]
    assert said == ["✅ new.jsession", "Добавлено воркеров: 1. Воркеров в проекте: 2."]

    async def nothing(*args):
        return 0

    monkeypatch.setattr(cli, "import_all", nothing)
    asyncio.run(cli.AddByPhoneFunc(_Storage(), ns()).import_files())
    assert said[-1].startswith("Нечего импортировать")


def test_the_bot_runs_a_new_instance_each_time():
    # a mailing's stats, a run's ledger or progress never outlive the run: resolve() makes a new instance
    import functools

    from bot.routers._common import resolve

    class _Func:
        def __init__(self, storage, settings):
            self.made_with = (storage, settings)

    functions = {"PmMailingFunc": functools.partial(_Func, "storage", "settings")}
    first, bot_function = resolve(functions, "pmmailing")
    second, _ = resolve(functions, "pmmailing")
    assert first is not second and first.made_with == ("storage", "settings") and bot_function.key == "pmmailing"


# --- auto-resume must not crash-loop the bot ------------------------------------------------

def test_auto_resume_gives_up_after_restarts_without_progress(monkeypatch, tmp_path):
    from bot.routers import scraping as router

    calls = _scrapes(monkeypatch)
    _checkpoint(tmp_path)  # 42 posts saved
    # the previous automatic resume already found 42 posts and was killed again at the same spot
    router.scrape._write_marker(conftest.home(), {"out_dir": str(tmp_path), "name": "n", "account": _ACC, "chat_id": 7,
                          "t_index": 42, "stalls": 1})
    bot = _SBot()
    asyncio.run(router.scrape.resume_after_restart(bot, conftest.Workspaces(_pool()), None))

    assert calls == [] and router.scrape._read_marker(conftest.home()) is None
    assert _sent(bot)[0].startswith("⚠️ Автопродолжение скрапа «n» отключено")


def test_restarts_after_a_clean_shutdown_dont_count_as_stalls(monkeypatch, tmp_path):
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager

    seen = []
    _scrapes(monkeypatch, lambda params: seen.append(router.scrape._read_marker(conftest.home())))
    _checkpoint(tmp_path)  # 42 posts, as in the marker: no progress since
    router.scrape._write_marker(conftest.home(), {"out_dir": str(tmp_path), "name": "n", "account": _ACC, "chat_id": 7,
                          "t_index": 42, "stalls": 1})
    # e.g. needrestart after a library upgrade
    asyncio.run(router.scrape.stop_for_shutdown(conftest.Workspaces(scrapes=JobManager())))
    router.common.shutting_down = False

    async def scenario():
        await router.scrape.resume_after_restart(_SBot(), conftest.Workspaces(_pool()), None)
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    [marker] = seen
    assert (marker["t_index"], marker["stalls"]) == (42, 1) and "clean_shutdown" not in marker


def test_auto_resume_with_progress_counts_afresh(monkeypatch, tmp_path):
    from bot.routers import scraping as router

    seen = []
    _scrapes(monkeypatch, lambda params: seen.append(router.scrape._read_marker(conftest.home())))
    _checkpoint(tmp_path)  # 42 posts now
    router.scrape._write_marker(conftest.home(), {"out_dir": str(tmp_path), "name": "n", "account": _ACC, "chat_id": 7,
                          "t_index": 10, "stalls": 1})

    async def scenario():
        await router.scrape.resume_after_restart(_SBot(), conftest.Workspaces(_pool()), None)
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    [marker] = seen  # what the running scrape's marker holds for the next restart
    assert (marker["t_index"], marker["stalls"]) == (42, 0)


def test_the_marker_is_written_atomically(tmp_path):
    import os

    from bot.routers import scraping as router

    router.scrape._write_marker(conftest.home(), {"name": "n"})
    assert router.scrape._read_marker(conftest.home()) == {"name": "n"}
    assert not os.path.exists(router.common.scraped_files.SCRAPE_MARKER + ".tmp")


def test_a_crashed_scrape_process_says_the_data_is_kept(monkeypatch, tmp_path):
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager

    def killed(params):
        raise RuntimeError("процесс скрапера аварийно завершился (код -9 — вероятно, не хватило памяти)")

    _scrapes(monkeypatch, killed)
    _checkpoint(tmp_path)  # what it saved before dying
    bot = _SBot()
    params = router.scrape._scrape_params({**_SCRAPE, "out_dir": str(tmp_path), "date_max": "31.01.2024"})
    asyncio.run(router.scrape._run_scrape(bot, 1, JobManager(), _pool(), None, params))
    text = _sent(bot)[-1]
    assert "не хватило памяти" in text and "Собранное сохранено" in text


# --- 4. profile / security functions on the picked workers only ---------------------------

class _PickCallback:
    """A callback on the picker's message: records alerts and the message's edits."""

    def __init__(self, message=None):
        self.message = message or _Msg()
        self.alerts = []
        self.message.edits = []

        async def edit_text(text, reply_markup=None):
            self.message.edits.append((text, reply_markup))
        self.message.edit_text = edit_text

    async def answer(self, text=None, show_alert=False):
        if text:
            self.alerts.append(text)


def _pick(value, state, pool, manager=None, functions=None):
    from bot.routers import profile

    callback = _PickCallback()
    asyncio.run(profile.workers_pick(callback, ChoiceCB(scope="wpick", value=value), state, pool,
                                     functions or {}, manager))
    return callback


def _seeded(*keys, **done):
    """A ledger already kept: these workers done by these functions ({key: [classnames]})."""
    from modules import profile_done, json_file

    json_file.save(profile_done.PATH, {"done": {key: dict.fromkeys(done.get(key, []), "2026-10-01") for key in keys},
                                       "values": {}})


def _jpool(*paths):
    """A pool of .jsession workers: sessions/a.session is user id "a", shown as @a."""
    pool = _pool(*paths)
    for path in paths:
        stem = os.path.basename(path).split(".")[0]
        pool.storage.jsessions_paths[path] = ns(account=ns(account=ns(
            user_id=stem, first_name=None, last_name=None, username=stem, phone_number="+1")))
    return pool


_A, _B = "sessions/a.session", "sessions/b.session"


def test_picker_ticks_the_new_workers_and_toggles():
    from bot.routers import profile
    from bot.states import PickWorkers

    _seeded("a", "b", a=["ChangeNameFunc"])  # a: renamed before, b: new
    pool, state, callback = _jpool(_A, _B), _State(), _Callback()
    asyncio.run(profile.picker_start(callback, FunctionCB(key="name"), state, pool))
    text, markup = callback.message.answers[0]  # in the screen's place (_Msg keeps an edit as an answer)
    assert state.state == PickWorkers.marks and text.startswith("✏️ Имя — на каких воркерах? Отмечено: 1 из 2")
    assert _buttons(markup)[:2] == ["⬜ @a ✓ 01.10", "✅ @b"]
    assert "стр 1/1" not in _buttons(markup)  # one page: no paging

    edited = _pick("0", state, pool).message.edits[0]
    assert "Отмечено: 2 из 2" in edited[0] and _buttons(edited[1])[:2] == ["✅ @a ✓ 01.10", "✅ @b"]
    assert _pick("none", state, pool).message.edits[0][0].startswith("✏️ Имя — на каких воркерах? Отмечено: 0 из 2")
    assert _pick("go", state, pool).alerts == ["Отметьте хотя бы одного воркера"]
    assert _pick("new", state, pool).message.edits[0][0].startswith("✏️ Имя — на каких воркерах? Отмечено: 1 из 2")
    assert _pick("all", state, pool).message.edits[0][0].startswith("✏️ Имя — на каких воркерах? Отмечено: 2 из 2")


def test_first_picker_marks_every_current_worker_done():
    from bot.routers import profile
    from modules import profile_done

    pool, callback = _jpool(_A, _B), _Callback()
    asyncio.run(profile.picker_start(callback, FunctionCB(key="photo"), _State(), pool))
    text, markup = callback.message.answers[0]
    assert "Все уже менялись" in text and _buttons(markup)[:2] == ["⬜ @a ✓", "⬜ @b ✓"]
    assert profile_done.load()["done"]["a"]["SetPasswordFunc"] == profile_done.SEEDED

    profile_done.seed(["c"])  # once only: a worker added later is new
    assert "c" not in profile_done.load()["done"]


def test_picker_notes_scraping_and_restricted_workers():
    from bot.routers import profile
    from modules import restricted_workers

    _seeded("a", "b")
    restricted_workers.save([_A])
    restricted_workers.save_status({_B: "01.11.2026"})
    pool, callback = _jpool(_A, _B), _Callback()
    pool.scraping = ns(path=_B, label="B")
    asyncio.run(profile.picker_start(callback, FunctionCB(key="bio"), _State(), pool))
    assert _buttons(callback.message.answers[0][1])[:2] == ["✅ @a ⛔", "✅ @b 🚫 до 01.11.2026 ⏳ занят скрапом"]


def test_picker_pages():
    from bot.routers import profile

    paths = [f"sessions/w{i:02}.session" for i in range(45)]
    _seeded(*[f"w{i:02}" for i in range(45)])
    pool, state, callback = _jpool(*paths), _State(), _Callback()
    asyncio.run(profile.picker_start(callback, FunctionCB(key="bio"), state, pool))
    buttons = _buttons(callback.message.answers[0][1])
    assert buttons[0] == "✅ @w00" and buttons[19] == "✅ @w19" and "📄 стр. 1/3" in buttons

    buttons = _buttons(_pick("next", state, pool).message.edits[0][1])
    assert buttons[0] == "✅ @w20" and "📄 стр. 2/3" in buttons
    _pick("prev", state, pool)
    buttons = _buttons(_pick("prev", state, pool).message.edits[0][1])  # wraps around to the last
    assert buttons[0] == "✅ @w40" and len([b for b in buttons if "@w" in b]) == 5
    assert _pick("page", state, pool).message.edits == []  # the page number is no button


def test_picked_workers_reach_the_job():
    from bot.routers import profile
    from bot.states import ProfileText

    _seeded("a", "b")
    pool, state = _jpool(_A, _B), _State()
    asyncio.run(profile.picker_start(_Callback(), FunctionCB(key="name"), state, pool))
    _pick("0", state, pool)
    callback = _pick("go", state, pool)
    assert state.state == ProfileText.text and "Пришлите имя" in callback.message.answers[0][0]

    seen = []

    class _Manager:
        active = False

        async def run(self, *args, only=None, **kw):
            seen.append(only)

    asyncio.run(profile.text_typed(_Msg(text="Иван"), state, pool, {"ChangeNameFunc": object}, _Manager()))
    assert seen == [[_B]]


def test_all_picked_runs_on_the_shown_workers_only():
    """«Все» is the workers the picker showed: one added meanwhile (by phone, tdata) was never picked."""
    from bot.routers import profile

    seen = []

    class _Manager:
        active = False

        async def run(self, *args, only=None, **kw):
            seen.append(only)

    _seeded("a", "b")
    pool, state = _jpool(_A, _B), _State()
    functions = {"HideLastSeenFunc": object}
    asyncio.run(profile.picker_start(_Callback(), FunctionCB(key="lastseen"), state, pool))
    _pick("go", state, pool, _Manager(), functions)
    assert seen == [[_A, _B]] and state.state is None


def test_delegate_keeps_only_the_picked_workers(monkeypatch):
    monkeypatch.setattr(WorkerPool, "_assert_workers", staticmethod(lambda sessions: None))
    pool = _pool("sessions/a.session", "sessions/b.session", "sessions/c.session")
    a, b, c = pool.workers
    pool.scraping = ns(path="sessions/c.session", label="C")  # not picked: no hold, no notice
    func, reports = ns(), []

    async def report(text):
        reports.append(text)

    async def job(f):
        pass

    ran = asyncio.run(pool.run(func, ns(risk="safe"), job, report, only=["sessions/b.session"]))
    assert ran is True and func.sessions == [b] and func.on_hold == [] and reports == []

    ran = asyncio.run(pool.run(func, ns(risk="safe", key="name"), job, report, only=["sessions/gone.session"]))
    assert ran is False and "недоступны" in reports[0]


# --- the chats' names, shown as soon as they are typed -----------------------------------------

def _named_chats(monkeypatch, names, error=None):
    """The real _lookup over chats named by `names` (raw -> title; any other is not found);
    `error`: the lookup itself fails with it. Returns the chat lists looked up."""
    import conftest
    from bot.routers import scraping as router
    from scraper.scrape import ChatInfo

    looked_up = []

    async def lookup_on(client, chats):
        looked_up.append(chats)
        if error is not None:
            raise error
        return [ChatInfo(c, f"«{names[c]}»", None) if c in names else ChatInfo(c, None, "нет такого")
                for c in chats]

    monkeypatch.setattr(router.common, "lookup", conftest.REAL_BOT_LOOKUP)
    monkeypatch.setattr(router.common, "lookup_on", lookup_on)
    monkeypatch.setattr(router.common, "borrow", _borrow_offline)
    return looked_up


async def _borrow_offline(pool, client, path, work, timeout):
    """delegation.borrow without connecting the stub clients: the account is busy meanwhile."""
    pool.borrowed[path] += 1
    try:
        return await work()
    finally:
        pool.borrowed[path] -= 1


def test_scrape_channels_are_shown_by_name_up_to_the_confirmation(monkeypatch):
    from bot.routers import scraping as router
    from bot.states import Scrape

    from modules import projects

    projects.add("n", ["@a", "@b"])  # a scrape is a project's: its checks name the chats on it
    _named_chats(monkeypatch, {"@a": "Канал А", "@b": "Канал Б"})
    state = _State({k: v for k, v in _SCRAPE.items() if k != "channels"})
    msg = _Msg(text="@a, @b")
    asyncio.run(router.scrape.scrape_channels(msg, state, _pool(), None))
    assert [text for text, _ in msg.answers] == [
        "🔎 Проверяю названия…", "1. «Канал А»\n👤 @a\n\n2. «Канал Б»\n👤 @b",
        "Пришлите дату «с» (ДД.ММ.ГГГГ или ГГГГ-ММ-ДД):"]
    assert msg.sent[0].deleted  # the 🔎 gives way to the names
    assert state.state == Scrape.date_min

    confirm = _Msg(text="31.01.2024")
    asyncio.run(router.scrape.scrape_run(confirm, state))
    # one block each, as on the project's card
    assert "— каналы:\n\n1. «Канал А»\n👤 @a\n\n2. «Канал Б»\n👤 @b\n\nПериод:" in confirm.answers[0][0]


def test_a_chat_not_found_asks_before_each_flow_goes_on(monkeypatch):
    from bot.routers import scraping as router
    from bot.states import Members, Scrape, Verify

    _named_chats(monkeypatch, {"@a": "Канал А"})
    flows = [(router.scrape.scrape_channels, Scrape.channels, Scrape.date_min, "Пришлите дату «с» (ДД"),
             # ▶️ collects the project's base at once (test_members)
             (functools.partial(router.members.members_chats, scrapes=None), Members.chats, None, None),
             (router.verify.verify_channel, Verify.channel, Verify.date_min, "Пришлите дату «с»")]
    for handler, asking, next_state, next_prompt in flows:
        state = _State({"account": _ACC})
        state.state = asking
        msg = _Msg(text="@typo")
        asyncio.run(handler(msg, state, _pool(), None))
        assert msg.answers[-2][0] == "1. @typo\n⚠️ нет такого"  # the list, then the question under it
        text, markup = msg.answers[-1]
        assert text == "Пришлите список заново или продолжите как есть:"
        assert _buttons(markup) == ["▶️ Продолжить всё равно", "✖️ Отмена"] and state.state == asking
        if next_state is None:
            continue

        go = _SCallback()
        asyncio.run(router.common.chats_ok(go, state, _pool(), None, None))
        assert state.state == next_state and go.message.answers[0][0].startswith(next_prompt)


def test_a_long_list_of_chats_goes_in_messages_within_telegram_s_limit(monkeypatch):
    from bot.routers import scraping as router
    from bot.states import Scrape

    chats = [f"@chan{i:03}" for i in range(40)]
    _named_chats(monkeypatch, {chat: "Т" * 120 for chat in chats})  # long titles: ~5 400 characters in all
    state = _State({"account": _ACC})
    state.state = Scrape.channels
    msg = _Msg(text=", ".join([*chats, "@typo"]))
    asyncio.run(router.scrape.scrape_channels(msg, state, _pool(), None))
    blocks = msg.answers[1:-1]  # after the 🔎, before the question
    assert len(blocks) > 1 and all(len(text) <= 4096 for text, _ in blocks)
    assert "\n\n".join(text for text, _ in blocks).count("\n👤 @chan") == 40
    assert _buttons(msg.answers[-1][1]) == ["▶️ Продолжить всё равно", "✖️ Отмена"]


def test_a_retyped_list_is_checked_again(monkeypatch):
    from bot.routers import scraping as router
    from bot.states import Scrape

    looked_up = _named_chats(monkeypatch, {"@a": "Канал А"})
    state = _State({"account": _ACC})
    state.state = Scrape.channels
    asyncio.run(router.scrape.scrape_channels(_Msg(text="@typo"), state, _pool(), None))
    asyncio.run(router.scrape.scrape_channels(_Msg(text="@a"), state, _pool(), None))
    assert looked_up == [["@typo"], ["@a"]] and state.state == Scrape.date_min
    assert state.data["channels"] == "@a" and state.data["chat_labels"] == ["«Канал А» (@a)"]


def test_a_stale_continue_button_says_so():
    from bot.routers import scraping as router

    go = _Callback()
    asyncio.run(router.common.chats_ok(go, _State(), None, None, None))
    assert go.alerts == ["Кнопка устарела — откройте раздел заново."] and not go.message.answers


def test_a_busy_account_is_not_asked_and_the_flow_goes_on(monkeypatch):
    from bot.routers import scraping as router
    from bot.states import Scrape

    looked_up = _named_chats(monkeypatch, {"@a": "Канал А"})
    pool = _pool()
    pool.polling = _ACC  # autoreply reads it
    state, msg = _State({"account": _ACC}), _Msg(text="@a")
    asyncio.run(router.scrape.scrape_channels(msg, state, pool, None))
    assert looked_up == [] and state.state == Scrape.date_min
    assert msg.answers[0][0] == "ℹ️ Названия не проверены: аккаунт сейчас занят."


def test_a_failed_lookup_goes_on_with_the_input_as_typed(monkeypatch):
    from bot.routers import scraping as router
    from bot.states import Scrape

    _named_chats(monkeypatch, {}, error=TimeoutError())
    state, msg = _State({**_SCRAPE, "channels": None}), _Msg(text="@a")
    asyncio.run(router.scrape.scrape_channels(msg, state, _pool(), None))
    status = msg.sent[0]  # the 🔎 becomes the reason
    assert status.edited == ["ℹ️ Названия не проверены: Telegram не ответил вовремя"] and not status.deleted
    assert state.state == Scrape.date_min and state.data["chat_labels"] is None

    confirm = _Msg(text="31.01.2024")
    asyncio.run(router.scrape.scrape_run(confirm, state))
    assert "— каналы:\n\n1. @a\n\nПериод:" in confirm.answers[0][0]  # never named: as typed


def test_a_check_cut_by_a_job_s_end_goes_on_with_the_input_as_typed(monkeypatch):
    """The names are asked on the account's own client: a job ending meanwhile disconnects it, and
    Telethon cancels the pending request — that check only, not the bot."""
    from bot.routers import scraping as router
    from bot.states import Scrape

    _named_chats(monkeypatch, {}, error=asyncio.CancelledError())
    state, msg, pool = _State({**_SCRAPE, "channels": None}), _Msg(text="@a"), _pool()
    asyncio.run(router.scrape.scrape_channels(msg, state, pool, None))
    assert msg.sent[0].edited == ["ℹ️ Названия не проверены: соединение прервано"]
    assert state.state == Scrape.date_min and not pool.busy(_ACC)


def test_an_empty_channel_list_is_asked_again(monkeypatch):
    from bot.routers import scraping as router
    from bot.states import Scrape

    looked_up = _named_chats(monkeypatch, {})
    state = _State({"account": _ACC})
    state.state = Scrape.channels
    msg = _Msg(text=" , ")
    asyncio.run(router.scrape.scrape_channels(msg, state, _pool(), None))
    assert looked_up == [] and state.state == Scrape.channels
    assert msg.answers == [("Не вижу ни одного канала или группы. Пришлите ещё раз:", None)]


def test_an_interrupted_scrape_is_offered_with_its_channels_names(monkeypatch, tmp_path):
    from bot.routers import scraping as router

    from modules import projects
    from scraper.scrape import ChatInfo

    _scrapes(monkeypatch)
    looked_up = _named_chats(monkeypatch, {"@a": "Канал А", "@b": "Канал Б"})
    projects.add("n", ["@a", "@b"])
    projects.remember("n", [ChatInfo("@a", "«Канал А»", None, -1001, "a"), ChatInfo("@b", "«Канал Б»", None)])
    _checkpoint(tmp_path)
    monkeypatch.setattr(router.common, "DEFAULT_OUT", str(tmp_path))
    msg = _SMsg()
    _project_scrape(router, msg, _State({}))
    # as the project's checks named them, one block each as on its card
    assert ("Каналы:\n\n1. «Канал А»\n👤 @a · 🆔 <code>-1001</code>\n\n2. «Канал Б»\n👤 @b\n\nПериод:"
            in msg.edits[0][0])
    assert looked_up == []  # no call to Telegram


def test_the_channels_names_ride_along_with_the_scrape(monkeypatch):
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager

    calls = _scrapes(monkeypatch)
    state = _State({**_SCRAPE, "date_max": "31.01.2024", "chat_labels": ["«Канал А» (@a)"]})
    asyncio.run(router.scrape.scrape_go(_SCallback(), ChoiceCB(scope="scrape_go", value="run"), state,
                                 _pool(), None, JobManager()))
    [params] = calls
    assert params.chat_labels == ["«Канал А» (@a)"]  # kept in its checkpoint


@pytest.mark.parametrize("module, handler, key", [("chat_broadcasts", "start", "comments"),
                                                  ("activity", "reactions_start", None),
                                                  ("activity", "poll_start", None), ("moderation", "ru_start", None),
                                                  ("moderation", "rm_start", None)])
def test_a_flow_starts_without_another_flow_s_leftovers(module, handler, key):
    import importlib

    router = importlib.import_module(f"bot.routers.{module}")
    state = _State({"trigger": "go!", "link": "https://t.me/old/1"})  # a trigger flow left midway
    args = (FunctionCB(key=key),) if key else ()
    asyncio.run(getattr(router, handler)(_Callback(), *args, state, _pool()))
    # the comments run would write that trigger to config.toml (_save_options)
    assert not {"trigger", "link"} & state.data.keys()


def test_the_contacts_and_profile_flows_start_without_leftovers(tmp_path, monkeypatch):
    from bot.routers import audience, profile

    monkeypatch.chdir(tmp_path)  # the profile picker reads stats/ (empty here)
    leftovers = {"trigger": "go!", "path": "assets/databases/G_posts.parquet"}
    state = _State(dict(leftovers))
    asyncio.run(audience.ask_base(_Msg(), state, []))
    assert not leftovers.keys() & state.data.keys()

    state = _State(dict(leftovers))
    pool = _pool()
    asyncio.run(profile._ask_workers(_Callback().message, state, "bio", pool))
    assert not leftovers.keys() & state.data.keys()


# (router, handler, button value, the step its keyboard is shown at, extra handler args)
_MID_FLOW = [
    ("mailing", "mail_skip", "yes", "PmMailing.path", ()),
    ("chat_broadcasts", "mode_pick", "0", "ChatPost.mode", ()),
    ("chat_broadcasts", "mention_pick", "yes", "ChatPost.mode", ("settings",)),
    ("chat_broadcasts", "mmode_pick", "admins", "ChatPost.mode", ("settings",)),
    ("join", "pick_mode", "1", "Join.mode", ()),
    ("join", "pick_captcha", "yes", "Join.mode", ()),
    ("moderation", "ru_reason", "0", "ReportUser.username", ()),
]


def _mid_flow_call(module, handler, value, state, extra):
    import importlib

    router = importlib.import_module(f"bot.routers.{module}")
    callback = _Callback()
    extras = [ns(trigger="go", messages_count=0) for _ in extra]
    asyncio.run(getattr(router, handler)(callback, ChoiceCB(scope="x", value=value), state, *extras))
    return callback.alerts + [text for text, _ in callback.message.answers]  # a stale tap: in a pop-up


@pytest.mark.parametrize("module, handler, value, step, extra", _MID_FLOW)
def test_an_old_mid_flow_button_leaves_another_flow_alone(module, handler, value, step, extra):
    from bot.states import Analysis

    state = _State({"path": "assets/databases/G_posts.parquet"})  # the analysis' file step
    state.state = Analysis.file
    replies = _mid_flow_call(module, handler, value, state, extra)
    # e.g. «Пропустить тех, кому уже писали?» would lead to a mailing to that posts file's people
    assert replies == ["Кнопка устарела — откройте раздел заново."]
    assert state.state is Analysis.file and state.data == {"path": "assets/databases/G_posts.parquet"}


@pytest.mark.parametrize("module, handler, value, step, extra", _MID_FLOW)
def test_a_mid_flow_button_works_at_its_own_step(module, handler, value, step, extra):
    import bot.states as states

    group, name = step.split(".")
    state = _State()
    state.state = getattr(getattr(states, group), name)
    replies = _mid_flow_call(module, handler, value, state, extra)
    assert replies and "устарел" not in replies[0]


@pytest.mark.parametrize("peer, by_phone, kept", [
    ("+7 999 123-45-67", True, "+79991234567"), ("79991234567", True, "+79991234567"), ("@durov", False, "@durov"),
    ("durov", False, "durov"), ("https://t.me/durov", False, "https://t.me/durov"), ("+12", False, "+12")])
def test_pm_to_a_person_sees_a_number_or_a_username(peer, by_phone, kept):
    """✉️ ЛС человеку asks no «по username / по номеру»: what was sent says which."""
    from bot.routers import pm_broadcast
    from bot.states import PmBroadcast

    state = _State()
    asyncio.run(pm_broadcast.got_peer(_Msg(text=peer), state))
    assert state.data == {"peer": kept, "by_phone": by_phone} and state.state == PmBroadcast.message
