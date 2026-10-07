"""Bot ↔ CLI parity: the delay setting, names/usernames from a list, scrape options in the
bot; the accounts list and proxies in the CLI."""

import asyncio
import io
import os
import types

import toml

from bot.callbacks import ChoiceCB
from bot.services.delegation import WorkerPool


def ns(**kw):
    return types.SimpleNamespace(**kw)


class _State:
    def __init__(self, data=None):
        self.data, self.state = dict(data or {}), None

    async def set_state(self, state):
        self.state = state

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_data(self):
        return dict(self.data)

    async def clear(self):
        self.data, self.state = {}, None


class _Msg:
    def __init__(self, text=None, document=None, content=b""):
        self.text, self.document, self.answers = text, document, []
        self.chat = ns(id=1)

        async def download(document):
            return io.BytesIO(content)

        self.bot = ns(download=download)

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs.get("reply_markup")))


class _Callback:
    def __init__(self):
        self.message = _Msg()

    async def answer(self, *a, **k):
        pass


def _buttons(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def _launches(monkeypatch=None):
    """A JobManager stand-in: records the kwargs each launched run() got."""
    runs = []

    class _Manager:
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
    from bot.routers import accounts

    settings = _settings(tmp_path, monkeypatch)
    (tmp_path / "config.toml").write_text("[broadcast\ndelay = [5, 10]\n")  # broken by hand meanwhile
    msg = _Msg(text="2-4")
    asyncio.run(accounts.delay_apply(msg, _State(), settings))
    assert "Не удалось сохранить config.toml" in msg.answers[0][0] and settings.delay == [5, 10]


def test_bot_delay_button(tmp_path, monkeypatch):
    from bot.routers import accounts

    settings = _settings(tmp_path, monkeypatch)
    state, callback = _State(), _Callback()
    asyncio.run(accounts.delay_start(callback, state, settings))
    assert "5–10 с" in callback.message.answers[0][0]

    for bad in ("abc", "1-2-3", "", "-5"):
        msg = _Msg(text=bad)
        asyncio.run(accounts.delay_apply(msg, state, settings))
        assert "Пришлите ещё раз" in msg.answers[0][0]
    assert settings.delay == [5, 10]

    msg = _Msg(text="7")
    asyncio.run(accounts.delay_apply(msg, state, settings))
    assert settings.delay == [7] and toml.load(tmp_path / "config.toml")["broadcast"]["delay"] == [7]


def test_bot_profile_pause_rejects_bad_input_and_saves_a_range(tmp_path, monkeypatch):
    from bot.routers import accounts

    settings = _settings(tmp_path, monkeypatch)
    state = _State()
    for bad in ("abc", "1-2-3", "", "-5"):
        msg = _Msg(text=bad)
        asyncio.run(accounts.profilepause_apply(msg, state, settings))
        assert "Пришлите ещё раз" in msg.answers[0][0]

    msg = _Msg(text="90 - 240")
    asyncio.run(accounts.profilepause_apply(msg, state, settings))
    assert settings.profile_pause == [90, 240]
    assert toml.load(tmp_path / "config.toml")["limits"]["profile_pause"] == [90, 240]


# --- 2. names / usernames from a list ------------------------------------------------------

def test_names_from_a_sent_txt(monkeypatch):
    from bot.routers import profile

    manager, runs = _launches()
    msg = _Msg(document=ns(file_size=20), content="Иван Петров\n\nАнна\n".encode())
    asyncio.run(profile.name_list(msg, _State(), ns(), {"ChangeNameFunc": object()}, manager))
    assert runs == [{"names": ["Иван Петров", "Анна"]}]


def test_names_button_from_assets(tmp_path, monkeypatch):
    from bot.routers import profile

    monkeypatch.setattr(profile, "NAMES_FILE", str(tmp_path / "names.txt"))
    (tmp_path / "names.txt").write_text("A\nB\nC\n")
    state = _State()
    asyncio.run(profile.name_start(_Callback(), state, _pool(), {}, None))
    _pick("all", state, _pool())
    callback = _pick("go", state, _pool())
    assert _buttons(callback.message.answers[0][1]) == [f"📄 Из {tmp_path / 'names.txt'} (3)"]

    manager, runs = _launches()
    asyncio.run(profile.name_from_file(_Callback(), _State(), ns(), {"ChangeNameFunc": object()}, manager))
    assert runs == [{"names": ["A", "B", "C"]}]


def test_typed_name_works_as_before():
    from bot.routers import profile

    manager, runs = _launches()
    asyncio.run(profile.name_run(_Msg(text="Иван Петров"), _State(), ns(), {"ChangeNameFunc": object()}, manager))
    assert runs == [{"first_name": "Иван", "last_name": "Петров"}]


def test_usernames_from_a_sent_txt():
    from bot.routers import profile

    manager, runs = _launches()
    msg = _Msg(document=ns(file_size=20), content=b"@one\ntwo\n")
    asyncio.run(profile.username_list(msg, _State(), ns(), {"ChangeUsernameFunc": object()}, manager))
    assert runs == [{"usernames": ["one", "two"]}]


def test_empty_list_is_asked_again():
    from bot.routers import profile

    manager, runs = _launches()
    msg = _Msg(document=ns(file_size=2), content=b"\n\n")
    asyncio.run(profile.username_list(msg, _State(), ns(), {"ChangeUsernameFunc": object()}, manager))
    assert runs == [] and "нет ни одной строки" in msg.answers[0][0]


# --- 3. scrape: account, its own slot, options, stop, continuing ----------------------------

_ACC = "sessions/w.jsession"
_SCRAPE = {"channels": "@a", "name": "n", "out_dir": "out", "date_min": "01.01.2024", "account": _ACC}


def _pool(*paths):
    """A worker pool whose storage holds these session paths (one by default)."""
    paths = paths or (_ACC,)
    clients = [object() for _ in paths]
    by_client = dict(zip(map(id, clients), paths))
    return WorkerPool(ns(sessions=clients, jsessions_paths={},
                         get_session_path=lambda c: by_client[id(c)]))


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
        self.bot, self.edited = bot or _SBot(), []

    async def edit_reply_markup(self, reply_markup=None):
        self.edited.append(reply_markup)


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
    monkeypatch.setattr(scraping, "new_files", lambda path, before: [])
    monkeypatch.setattr(router, "build_credentials", lambda client: None)
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
    asyncio.run(router.scrape_run(msg, state))
    assert calls == [] and _buttons(msg.answers[0][1]) == ["▶️ Запустить", "⚙️ Дополнительно"]

    go = _SCallback()
    asyncio.run(router.scrape_go(go, ChoiceCB(scope="scrape_go", value="run"), state, _pool(), None, scrapes))
    [params] = calls
    assert params.keyword == "" and params.with_reactors and params.with_comments and params.resume is False
    assert params.account == _ACC
    assert seen == [(True, False)]  # the scraper's slot is taken, the bot's task slot stays free
    assert not scrapes.active and any("Скрап запущен на аккаунте" in t for t in _sent(go.bot))


def test_scrape_options_toggle_in_place_and_reach_the_run(monkeypatch):
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager
    from bot.states import Scrape

    calls = _scrapes(monkeypatch)
    state = _State({**_SCRAPE, "date_max": "31.01.2024"})
    state.state = Scrape.confirm

    more = _SCallback()
    asyncio.run(router.scrape_go(more, ChoiceCB(scope="scrape_go", value="more"), state, _pool(), None,
                                 JobManager()))
    assert _buttons(more.message.answers[0][1]) == [
        "💬 Комментарии ✅", "❤️ Реакции ✅ (медленно)", "👥 База участников ✅",
        "🔎 Фильтр по слову: —", "🔢 Максимум постов: все", "▶️ Запустить"]

    toggle = _SCallback()
    asyncio.run(router.scrape_option(toggle, ChoiceCB(scope="scrape_opt", value="reactors"), state,
                                     _pool(), None, JobManager()))
    assert _buttons(toggle.message.edited[0])[1] == "❤️ Реакции ❌ (медленно)"  # edited, not resent

    ask = _SCallback()
    asyncio.run(router.scrape_option(ask, ChoiceCB(scope="scrape_opt", value="keyword"), state,
                                     _pool(), None, JobManager()))
    assert state.state == Scrape.keyword
    asyncio.run(router.scrape_keyword(_Msg(text="крипта"), state))
    asyncio.run(router.scrape_option(_SCallback(), ChoiceCB(scope="scrape_opt", value="max"), state,
                                     _pool(), None, JobManager()))
    bad = _Msg(text="abc")
    asyncio.run(router.scrape_max(bad, state))
    assert state.state == Scrape.max_messages and "число" in bad.answers[0][0]
    asyncio.run(router.scrape_max(_Msg(text="500"), state))
    assert state.state == Scrape.confirm

    asyncio.run(router.scrape_option(_SCallback(), ChoiceCB(scope="scrape_opt", value="run"), state,
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
    params = router._scrape_params({**_SCRAPE, "date_max": "31.01.2024"})
    asyncio.run(router._run_scrape(bot, 1, JobManager(), _pool(), None, params))
    assert any("с тем же именем и папкой" in t for t in _sent(bot))
    assert router._read_marker() == {"out_dir": "out", "name": "n", "account": _ACC, "chat_id": 1}


def test_the_stop_button_stops_and_drops_the_marker(monkeypatch):
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager

    async def press_stop(params):
        await router.scrape_stop(_SCallback())  # the operator's ⏹, mid-run
        assert params.stop.is_set()
        raise SystemExit(1)  # the scrape checkpoints and exits

    _scrapes(monkeypatch, press_stop)
    bot = _SBot()
    params = router._scrape_params({**_SCRAPE, "date_max": "31.01.2024"})
    asyncio.run(router._run_scrape(bot, 1, JobManager(), _pool(), None, params))
    assert any(t.startswith("⏹ Скрап остановлен") for t in _sent(bot))
    assert router._read_marker() is None and router._job_stop is None


def test_a_bot_shutdown_checkpoints_silently_and_keeps_the_marker(monkeypatch):
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager

    async def shutdown(params):
        await router.stop_for_shutdown()
        assert params.stop.is_set()
        raise SystemExit(1)

    _scrapes(monkeypatch, shutdown)
    bot = _SBot()
    params = router._scrape_params({**_SCRAPE, "date_max": "31.01.2024"})
    asyncio.run(router._run_scrape(bot, 1, JobManager(), _pool(), None, params))
    assert not any("прерван" in t or "остановлен" in t for t in _sent(bot))  # the bot is going down
    assert router._read_marker() is not None


def test_a_finished_scrape_drops_the_marker(monkeypatch):
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager

    _scrapes(monkeypatch)
    params = router._scrape_params({**_SCRAPE, "date_max": "31.01.2024"})
    asyncio.run(router._run_scrape(_SBot(), 1, JobManager(), _pool(), None, params))
    assert router._read_marker() is None


def test_a_worker_in_a_bot_job_is_not_scraped_on(monkeypatch):
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager

    calls, bot, scrapes, pool = _scrapes(monkeypatch), _SBot(), JobManager(), _pool()
    pool.in_job = pool.workers  # a mailing runs on it
    params = router._scrape_params({**_SCRAPE, "date_max": "31.01.2024"})
    asyncio.run(router._run_scrape(bot, 1, scrapes, pool, None, params))
    assert calls == [] and _sent(bot) == [router.WORKER_BUSY]
    assert not scrapes.active and router._read_marker() is None and pool.scraping is None


def test_a_scraping_worker_is_kept_out_of_bot_jobs_until_the_scrape_ends(monkeypatch):
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager

    pool, during = _pool(), []

    def check(params):
        during.append(pool.scraping.path)
        raise RuntimeError("process killed")  # it is released on an error too

    _scrapes(monkeypatch, check)
    bot = _SBot()
    params = router._scrape_params({**_SCRAPE, "date_max": "31.01.2024"})
    asyncio.run(router._run_scrape(bot, 1, JobManager(), pool, None, params))
    assert during == [_ACC] and pool.scraping is None
    assert any("без этого воркера" in t for t in _sent(bot))


def test_a_failed_marker_write_frees_the_slot_and_the_worker(monkeypatch):
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager

    def no_space(marker):
        raise OSError(28, "No space left on device")

    calls, bot, scrapes, pool = _scrapes(monkeypatch), _SBot(), JobManager(), _pool()
    monkeypatch.setattr(router, "_write_marker", no_space)
    params = router._scrape_params({**_SCRAPE, "date_max": "31.01.2024"})
    asyncio.run(router._run_scrape(bot, 1, scrapes, pool, None, params))
    assert calls == [] and any("No space left" in t for t in _sent(bot))
    assert not scrapes.active and pool.scraping is None and router._job_stop is None


def test_a_gone_account_is_reported_and_nothing_runs(monkeypatch):
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager

    calls = _scrapes(monkeypatch)
    bot = _SBot()
    params = router._scrape_params({**_SCRAPE, "date_max": "31.01.2024", "account": "sessions/gone.jsession"})
    asyncio.run(router._run_scrape(bot, 1, JobManager(), _pool(), None, params))
    assert calls == [] and "не найден" in _sent(bot)[0]


# --- the account question: workers and personal accounts ------------------------------------

def test_one_account_is_taken_without_asking(tmp_path):
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager
    from bot.states import Scrape

    state = _State({"name": "n"})
    msg = _SMsg(text=str(tmp_path))
    asyncio.run(router.scrape_out(msg, state, _pool(), None, JobManager()))
    assert state.data["account"] == _ACC and state.state == Scrape.channels
    assert msg.answers[0][0].startswith("Каналы/группы")


def test_a_personal_account_is_offered_marked_and_warned_about(tmp_path):
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager
    from bot.states import Members

    personal = ns(sessions=[object()], jsessions_paths={},
                  get_session_path=lambda c: "personal_sessions/me.jsession")
    state = _State()
    start = _SCallback()
    asyncio.run(router.members_start(start, state, _pool(), personal, JobManager()))
    assert _buttons(start.message.answers[0][1]) == ["👤 me.jsession", "🤖 w.jsession"]  # personal first

    pick = _SCallback()
    asyncio.run(router.account_pick(pick, ChoiceCB(scope="scr_acc", value="0"), state, _pool(), personal,
                                    JobManager()))
    assert state.data["account"] == "personal_sessions/me.jsession" and state.state == Members.chats
    warning = pick.message.answers[0][0]
    assert warning.startswith("⚠️ Это ваш личный аккаунт") and "только до тех, у кого есть username" in warning


def test_verify_on_a_personal_account_warns_only_about_limits():
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager

    personal = ns(sessions=[object()], jsessions_paths={},
                  get_session_path=lambda c: "personal_sessions/me.jsession")
    state = _State()
    asyncio.run(router.verify_start(_SCallback(), state, _pool(), personal, JobManager()))
    pick = _SCallback()
    asyncio.run(router.account_pick(pick, ChoiceCB(scope="scr_acc", value="0"), state, _pool(), personal,
                                    JobManager()))
    warning = pick.message.answers[0][0]
    assert warning.startswith("⚠️ Это ваш личный аккаунт") and "username" not in warning  # no base here


def test_the_verify_stop_says_stopped(monkeypatch, tmp_path):
    from bot.routers import scraping as router
    from bot.services import scraping
    from bot.services.jobs import JobManager

    async def stopped(creds, params):
        await router.scrape_stop(_SCallback())
        raise SystemExit(1)  # verify ends as interrupted

    monkeypatch.setattr(scraping, "do_verify", stopped)
    monkeypatch.setattr(router, "build_credentials", lambda client: None)
    msg = _SMsg()
    asyncio.run(router._run_verify(msg, JobManager(), _pool(), ns(client=None, label="w", path=_ACC, personal=False),
                                   str(tmp_path / "x_posts.parquet"), "@a", "01.01.2024", "31.01.2024"))
    assert _buttons(msg.answers[0][1]) == ["📊 Прогресс", "⏹ Стоп"]
    assert msg.answers[-1][0] == "⏹ Верификация остановлена." and router._job_stop is None


def test_a_stale_account_button_says_so():
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager

    pick = _SCallback()
    asyncio.run(router.account_pick(pick, ChoiceCB(scope="scr_acc", value="0"), _State(), _pool(), None,
                                    JobManager()))
    assert pick.message.answers[0][0] == "Флоу устарел, начните заново."


# --- continuing an interrupted scrape from the bot ----------------------------------------

def _checkpoint(out_dir, name="n", account=_ACC):
    import json

    ckpt = out_dir / f"{name}_partial" / "checkpoint"
    ckpt.mkdir(parents=True)
    meta = {"name": name, "channels": ["@a", "@b"], "keyword": "", "t_index": 42,
            "date_min": "2024-01-01T00:00:00+00:00", "date_max": "2024-01-31T23:59:59+00:00",
            "with_reactors": False, "channel_index": 1, "last_id": 9}
    if account is not None:
        meta["account"] = account
    (ckpt / "resume.json").write_text(json.dumps(meta))


def test_scrape_offers_to_continue_an_interrupted_one_on_its_account(monkeypatch, tmp_path):
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager
    from bot.states import Scrape

    calls = _scrapes(monkeypatch)
    _checkpoint(tmp_path)
    state = _State({"name": "n"})
    msg = _SMsg(text=str(tmp_path))
    asyncio.run(router.scrape_out(msg, state, _pool(), None, JobManager()))

    text, markup = msg.answers[0]
    assert state.state == Scrape.resume and "Уже собрано постов: 42" in text and "@a, @b" in text
    assert _buttons(markup)[0] == "▶️ Продолжить"

    asyncio.run(router.scrape_resume(_SCallback(), ChoiceCB(scope="scrape_resume", value="continue"),
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
    asyncio.run(router.scrape_resume(_SCallback(), ChoiceCB(scope="scrape_resume", value="continue"),
                                     state, _pool(), None, JobManager()))
    [params] = calls  # one account: taken at once, then continued on it
    assert params.resume and params.account == _ACC


def test_scrape_start_fresh_asks_for_the_account_then_channels(monkeypatch, tmp_path):
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager
    from bot.states import Scrape

    calls = _scrapes(monkeypatch)
    _checkpoint(tmp_path)
    state = _State({"name": "n", "out_dir": str(tmp_path)})
    state.state = Scrape.resume
    fresh = _SCallback()
    asyncio.run(router.scrape_resume(fresh, ChoiceCB(scope="scrape_resume", value="fresh"),
                                     state, _pool(), None, JobManager()))
    assert calls == [] and state.state == Scrape.channels
    assert fresh.message.answers[0][0].startswith("Каналы/группы")


def test_a_new_scrape_drops_an_abandoned_ones_settings():
    from bot.routers import scraping as router
    from bot.states import Scrape

    state = _State({"keyword": "крипта", "max_messages": 50, "comments": False})
    asyncio.run(router.scrape_start(_SCallback(), state, _pool(), None))
    assert state.state == Scrape.name and state.data == {}  # no hidden filter on the new run


def test_a_broken_marker_reads_as_none(monkeypatch, tmp_path):
    from bot.routers import scraping as router

    monkeypatch.setattr(router, "MARKER_PATH", str(tmp_path / "bot_scrape.json"))
    (tmp_path / "bot_scrape.json").write_text("{half-writ")
    assert router._read_marker() is None


def test_a_restart_continues_the_marked_scrape(monkeypatch, tmp_path):
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager

    calls = _scrapes(monkeypatch)
    _checkpoint(tmp_path)
    router._write_marker({"out_dir": str(tmp_path), "name": "n", "account": _ACC, "chat_id": 7})
    bot = _SBot()

    async def scenario():
        await router.resume_after_restart(bot, JobManager(), _pool(), None)
        await asyncio.sleep(0.05)  # it runs in the background

    asyncio.run(scenario())
    [params] = calls
    assert params.resume and params.account == _ACC and params.channels == ["@a", "@b"]
    assert _sent(bot)[0].startswith("🔄 Бот перезапущен")


def test_a_restart_without_a_checkpoint_drops_a_stale_marker(monkeypatch, tmp_path):
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager

    calls = _scrapes(monkeypatch)
    router._write_marker({"out_dir": str(tmp_path), "name": "n", "account": _ACC, "chat_id": 7})
    asyncio.run(router.resume_after_restart(_SBot(), JobManager(), _pool(), None))
    assert calls == [] and router._read_marker() is None


def test_a_restart_drops_the_marker_of_a_gone_account(monkeypatch, tmp_path):
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager

    calls = _scrapes(monkeypatch)
    gone = "sessions/gone.jsession"
    _checkpoint(tmp_path, account=gone)
    router._write_marker({"out_dir": str(tmp_path), "name": "n", "account": gone, "chat_id": 7})
    bot = _SBot()

    async def scenario():
        await router.resume_after_restart(bot, JobManager(), _pool(), None)
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    assert calls == [] and router._read_marker() is None  # no retry on the next restart
    assert any("не найден" in text for text in _sent(bot))


def test_a_restart_that_returns_early_still_counts_the_stall(monkeypatch, tmp_path):
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager

    calls = _scrapes(monkeypatch)
    _checkpoint(tmp_path)  # t_index 42, as in the marker: no progress since
    router._write_marker({"out_dir": str(tmp_path), "name": "n", "account": _ACC, "chat_id": 7,
                          "t_index": 42, "stalls": 0})
    pool = _pool()
    pool.in_job = pool.workers  # its worker is busy: _run_scrape returns before its own marker

    async def scenario():
        await router.resume_after_restart(_SBot(), JobManager(), pool, None)
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    assert calls == [] and router._read_marker()["stalls"] == 1


# --- 4. CLI: accounts list, proxies ---------------------------------------------------------

def test_cli_accounts_list_by_username(monkeypatch):
    from functions import accounts

    class _Storage:
        jsessions_paths = {}
        sessions = ["c2", "c10", "c1", "dead"]

        def get_session_path(self, client):
            return None

        async def fetch_me(self, client, timeout=20):
            if client == "dead":
                return None
            n = client[1:]
            return ns(first_name="W", last_name=None, username=f"Curator{n}", phone=n, id=int(n))

    printed = []
    monkeypatch.setattr(accounts.console, "print", lambda table, *a, **k: printed.append(table))
    asyncio.run(accounts.AccountsFunc(_Storage(), ns()).execute())

    usernames = list(printed[0].columns[2].cells)
    assert usernames == ["@Curator1", "@Curator2", "@Curator10", "—"]


def test_cli_set_proxies_reconnects_rebuilt_clients(tmp_path, monkeypatch):
    from functions import set_proxies

    (tmp_path / "proxies.txt").write_text("socks5://u:p@1.2.3.4:1080\n")
    monkeypatch.setattr(set_proxies.Prompt, "ask", lambda *a, **k: str(tmp_path / "proxies.txt"))
    monkeypatch.setattr(set_proxies.console, "print", lambda *a, **k: None)

    class _Client:
        disconnected = False

        async def disconnect(self):
            self.disconnected = True

    old, new, checked = _Client(), _Client(), []

    class _Storage:
        initialize = True
        sessions = []
        full_sessions = {"a.jsession": old}

        def apply_proxies(self, proxies):
            assert proxies[0].ip == "1.2.3.4"
            self.full_sessions = {"a.jsession": new}
            return {"accounts": 1, "proxies_used": 1, "string_sessions_skipped": 0}

        async def check_session(self, client, path):
            checked.append((client, path))

    asyncio.run(set_proxies.SetProxiesFunc(_Storage(), ns()).execute())
    assert old.disconnected and checked == [(new, "a.jsession")]


def test_cli_photo_one_path_or_random_folder(tmp_path, monkeypatch):
    from functions import change_profile_photo as cpp

    photo = tmp_path / "me.jpg"
    photo.write_bytes(b"x")
    fn = cpp.ChangeProfilePhotoFunc(ns(sessions=[]), ns())
    fn.ask_accounts_count = lambda: None
    runs = []

    async def run(report, photo_path=None):
        runs.append(photo_path)

    fn.run = run
    for answers in ([f"'{photo}'"], ["missing.jpg", ""]):  # quoted drag-and-drop; a typo re-asked
        it = iter(answers)
        monkeypatch.setattr(cpp.console, "input", lambda *a, **k: next(it))
        monkeypatch.setattr(cpp.console, "print", lambda *a, **k: None)
        asyncio.run(fn.execute())
    assert runs == [str(photo), None]


def test_cli_menu_is_the_bot_s_menu(monkeypatch):
    import main
    from bot.services.registry import BOT_FUNCTIONS, CLI_EXTRAS

    class Unknown:
        """Some new thing"""

    functions = [(type(f.classname, (), {})(), "doc") for f in BOT_FUNCTIONS + CLI_EXTRAS]
    functions.append((Unknown(), Unknown.__doc__))
    entries = main.menu_entries(functions)

    assert [e[2] for e in entries[:2]] == ["ЛС по базе (со статой)", "ЛС одному получателю"]
    assert len(entries) == len(functions)  # nothing lost, nothing repeated
    assert entries[-1][0] == "Прочее" and entries[-1][2] == "Some new thing"
    workers = [e[2] for e in entries if e[0] == "🤖 Воркеры"]
    assert workers == ["Список аккаунтов", "Прокси"]

    printed = []
    monkeypatch.setattr(main.console, "print", lambda text="", *a, **k: printed.append(text))
    main.print_menu(entries)
    assert any("[1] ⚠️ [bold white]ЛС по базе (со статой)[/] — личные сообщения" in p for p in printed)


def test_run_instance_runs_the_chosen_function():
    from modules.storages.functions_storage import FunctionsStorage

    ran = []
    fs = FunctionsStorage.__new__(FunctionsStorage)
    fs.run_instance(ns(execute=lambda: ran.append(1)))
    assert ran == [1]


# --- auto-resume must not crash-loop the bot ------------------------------------------------

def test_auto_resume_gives_up_after_restarts_without_progress(monkeypatch, tmp_path):
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager

    calls = _scrapes(monkeypatch)
    _checkpoint(tmp_path)  # 42 posts saved
    # the previous automatic resume already found 42 posts and was killed again at the same spot
    router._write_marker({"out_dir": str(tmp_path), "name": "n", "account": _ACC, "chat_id": 7,
                          "t_index": 42, "stalls": 1})
    bot = _SBot()
    asyncio.run(router.resume_after_restart(bot, JobManager(), _pool(), None))

    assert calls == [] and router._read_marker() is None
    assert _sent(bot)[0].startswith("⚠️ Автопродолжение скрапа «n» отключено")


def test_auto_resume_with_progress_counts_afresh(monkeypatch, tmp_path):
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager

    seen = []
    _scrapes(monkeypatch, lambda params: seen.append(router._read_marker()))
    _checkpoint(tmp_path)  # 42 posts now
    router._write_marker({"out_dir": str(tmp_path), "name": "n", "account": _ACC, "chat_id": 7,
                          "t_index": 10, "stalls": 1})

    async def scenario():
        await router.resume_after_restart(_SBot(), JobManager(), _pool(), None)
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    [marker] = seen  # what the running scrape's marker holds for the next restart
    assert (marker["t_index"], marker["stalls"]) == (42, 0)


def test_the_marker_is_written_atomically(tmp_path):
    import os

    from bot.routers import scraping as router

    router._write_marker({"name": "n"})
    assert router._read_marker() == {"name": "n"} and not os.path.exists(router.MARKER_PATH + ".tmp")


def test_a_crashed_scrape_process_says_the_data_is_kept(monkeypatch, tmp_path):
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager

    def killed(params):
        raise RuntimeError("процесс скрапера аварийно завершился (код -9 — вероятно, не хватило памяти)")

    _scrapes(monkeypatch, killed)
    _checkpoint(tmp_path)  # what it saved before dying
    bot = _SBot()
    params = router._scrape_params({**_SCRAPE, "out_dir": str(tmp_path), "date_max": "31.01.2024"})
    asyncio.run(router._run_scrape(bot, 1, JobManager(), _pool(), None, params))
    text = _sent(bot)[-1]
    assert "не хватило памяти" in text and "Собранное сохранено" in text


def test_a_sticker_for_the_output_folder_is_asked_again(tmp_path):
    from bot.routers import scraping as router
    from bot.services.jobs import JobManager

    state = _State({"name": "n"})
    msg = _SMsg(text=None)  # a sticker / photo: no text
    asyncio.run(router.scrape_out(msg, state, _pool(), None, JobManager()))
    assert "out_dir" not in state.data and msg.answers[0][0] == "Ожидается текст. Попробуйте ещё раз."


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
    asyncio.run(profile.name_start(callback, state, pool, {}, None))
    text, markup = callback.message.answers[0]
    assert state.state == PickWorkers.choose and "Отмечено: 1 из 2" in text
    assert _buttons(markup)[:2] == ["⬜ @a ✓ 01.10", "✅ @b"]
    assert "стр 1/1" not in _buttons(markup)  # one page: no paging

    edited = _pick("0", state, pool).message.edits[0]
    assert "Отмечено: 2 из 2" in edited[0] and _buttons(edited[1])[:2] == ["✅ @a ✓ 01.10", "✅ @b"]
    assert _pick("none", state, pool).message.edits[0][0].startswith("На каких воркерах? Отмечено: 0 из 2")
    assert _pick("go", state, pool).alerts == ["Отметьте хотя бы одного воркера"]
    assert _pick("new", state, pool).message.edits[0][0].startswith("На каких воркерах? Отмечено: 1 из 2")
    assert _pick("all", state, pool).message.edits[0][0].startswith("На каких воркерах? Отмечено: 2 из 2")


def test_first_picker_marks_every_current_worker_done():
    from bot.routers import profile
    from modules import profile_done

    pool, callback = _jpool(_A, _B), _Callback()
    asyncio.run(profile.photo_start(callback, _State(), pool, {}, None))
    text, markup = callback.message.answers[0]
    assert "Новых нет" in text and _buttons(markup)[:2] == ["⬜ @a ✓", "⬜ @b ✓"]
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
    asyncio.run(profile.bio_start(callback, _State(), pool, {}, None))
    assert _buttons(callback.message.answers[0][1])[:2] == ["✅ @a ⛔", "✅ @b 🚫 до 01.11.2026 ⏳ скрап"]


def test_picker_pages():
    from bot.routers import profile

    paths = [f"sessions/w{i:02}.session" for i in range(45)]
    _seeded(*[f"w{i:02}" for i in range(45)])
    pool, state, callback = _jpool(*paths), _State(), _Callback()
    asyncio.run(profile.bio_start(callback, state, pool, {}, None))
    buttons = _buttons(callback.message.answers[0][1])
    assert buttons[0] == "✅ @w00" and buttons[19] == "✅ @w19" and "стр 1/3" in buttons

    buttons = _buttons(_pick("next", state, pool).message.edits[0][1])
    assert buttons[0] == "✅ @w20" and "стр 2/3" in buttons
    _pick("prev", state, pool)
    buttons = _buttons(_pick("prev", state, pool).message.edits[0][1])  # wraps around to the last
    assert buttons[0] == "✅ @w40" and len([b for b in buttons if "@w" in b]) == 5
    assert _pick("page", state, pool).message.edits == []  # the page number is no button


def test_picked_workers_reach_the_job():
    from bot.routers import profile
    from bot.states import ChangeName

    _seeded("a", "b")
    pool, state = _jpool(_A, _B), _State()
    asyncio.run(profile.name_start(_Callback(), state, pool, {}, None))
    _pick("0", state, pool)
    callback = _pick("go", state, pool)
    assert state.state == ChangeName.manual and "Введите имя" in callback.message.answers[0][0]

    seen = []

    class _Manager:
        async def run(self, *args, only=None, **kw):
            seen.append(only)

    asyncio.run(profile.name_run(_Msg(text="Иван"), state, pool, {"ChangeNameFunc": object()}, _Manager()))
    assert seen == [[_B]]


def test_all_picked_runs_on_the_whole_pool():
    from bot.routers import profile

    seen = []

    class _Manager:
        async def run(self, *args, only=None, **kw):
            seen.append(only)

    _seeded("a", "b")
    pool, state = _jpool(_A, _B), _State()
    functions = {"HideLastSeenFunc": object()}
    asyncio.run(profile.lastseen_start(_Callback(), state, pool, functions, _Manager()))
    _pick("go", state, pool, _Manager(), functions)
    assert seen == [None] and state.state is None


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

    ran = asyncio.run(pool.run(func, ns(risk="safe"), job, report, only=["sessions/gone.session"]))
    assert ran is False and "недоступны" in reports[0]
