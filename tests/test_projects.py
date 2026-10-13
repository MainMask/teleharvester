"""Projects (modules/projects.py): the file, its backups and names, a project's files, and the
bot's and the CLI's projects screens on the same model, a project's scrape included."""

import asyncio
import inspect
import json
import os

import pandas as pd
import pytest

from bot.callbacks import ChoiceCB
from modules import json_file, projects, scraped_files
from test_scraper_menu import FakeSettings, FakeStorage, _PICKED, _answers
from test_sync import _Msg, _SCallback, _SMsg, _State, _buttons, _pool, _scrapes


@pytest.fixture(autouse=True)
def _bases_dir(tmp_path, monkeypatch):
    """The bases folder is the test's own: no test reads the real assets/databases."""
    monkeypatch.setattr(scraped_files, "BASES_DIR", str(tmp_path))


def _posts(folder, name, mtime, channels=None, groups=("@a",)):
    """A finished scrape's posts file; channels: as the scrape stores them in the file."""
    df = pd.DataFrame({"Group": list(groups), "Message ID": range(len(groups))}) if groups else pd.DataFrame({"x": [1]})
    if channels is not None:
        df.attrs["channels"] = channels
    path = folder / f"{name}_posts_01.01.2024-31.01.2024.parquet"
    df.to_parquet(path)
    os.utime(path, (mtime, mtime))
    return path


def _write_parquet(path, rows=2):
    pd.DataFrame({"a": range(rows)}).to_parquet(path)


def _checkpoint(folder, name, t_index=7):
    ckpt = folder / f"{name}_partial" / "checkpoint"
    ckpt.mkdir(parents=True)
    (ckpt / "resume.json").write_text(json.dumps({
        "name": name, "channels": ["@a", "@b"], "t_index": t_index,
        "date_min": "2024-01-01T00:00:00+00:00", "date_max": "2024-01-31T23:59:59+00:00"}))


# --- the file -----------------------------------------------------------------------------

def test_no_file_means_no_projects_and_nothing_is_written():
    assert projects.load() == []
    assert not os.path.exists(projects.PATH)  # merely reading never creates it


def test_add_change_remove_round_trip():
    projects.add("  GreenBase ", ["@a", "t.me/b"])
    projects.add("Neo", ["@n"])
    assert projects.load() == [projects.Project("GreenBase", ["@a", "t.me/b"]), projects.Project("Neo", ["@n"])]

    projects.set_channels("Neo", ["@x", "@y"])
    projects.remove("GreenBase")
    assert projects.load() == [projects.Project("Neo", ["@x", "@y"])]
    with open(projects.PATH, encoding="utf-8") as file:
        assert json.load(file) == [{"name": "Neo", "channels": ["@x", "@y"]}]


def test_every_write_backs_up_the_previous_file_first():
    projects.add("A", ["@a"])
    assert not os.path.exists(projects.PATH + ".bak")  # nothing to back up before the first write
    with open(projects.PATH, encoding="utf-8") as file:
        first = file.read()

    projects.add("B", ["@b"])
    with open(projects.PATH + ".bak", encoding="utf-8") as file:
        assert file.read() == first
    projects.remove("B")
    with open(projects.PATH + ".bak", encoding="utf-8") as file:
        assert [p["name"] for p in json.load(file)] == ["A", "B"]


@pytest.mark.parametrize("content", ["{broken", '{"name": "A"}', '[{"channels": []}]'])
def test_an_unreadable_file_is_reported_and_never_overwritten(content):
    with open(projects.PATH, "w", encoding="utf-8") as file:
        file.write(content)
    with pytest.raises(ValueError, match="не читается"):
        projects.load()
    with pytest.raises(ValueError):
        projects.add("A", ["@a"])
    with open(projects.PATH, encoding="utf-8") as file:
        assert file.read() == content


@pytest.mark.parametrize("name, problem", [
    ("", "пустое"), ("x" * (projects.MAX_NAME + 1), "длиннее"), ("a/b", "без /"), ("a\\b", "без /"),
    ("..", "без /"), ("greenbase", "уже есть"), ("my_posts_x", "«_posts»"), ("a_partial", "«_partial»"),
])
def test_bad_names_are_refused(name, problem):
    projects.add("GreenBase", ["@a"])
    assert problem in projects.name_problem(name, projects.load())
    with pytest.raises(ValueError, match=problem):
        projects.add(name, ["@a"])


def test_a_project_needs_channels():
    with pytest.raises(ValueError, match="канал"):
        projects.add("A", [])
    projects.add("A", ["@a"])
    with pytest.raises(ValueError, match="канал"):
        projects.set_channels("A", [])
    with pytest.raises(ValueError, match="уже нет"):
        projects.set_channels("gone", ["@a"])


# --- a project's files ----------------------------------------------------------------------

def test_project_files_are_its_own_by_exact_name(tmp_path, monkeypatch):
    monkeypatch.setattr(scraped_files, "BASES_DIR", str(tmp_path))
    span = "01.01.2024-31.01.2024"
    for stem in (f"Green_posts_{span}", f"Green_participants_{span}", f"Green_reactors_{span}",
                 "Green_participants_members", f"Green_posts_{span}_missed",   # a verify result: not its
                 f"Green2_posts_{span}", f"Green_links_{span}"):                # another project's; an analysis
        _write_parquet(tmp_path / f"{stem}.parquet")

    labels = sorted(label for _, label in scraped_files.project_files("Green"))
    assert labels == [f"participants · {span} · 2", "participants · members · 2",
                      f"posts · {span} · 2", f"reactors · {span} · 2"]
    assert [label for _, label in scraped_files.project_files("Green2")] == [f"posts · {span} · 2"]
    assert scraped_files.project_files("Gr[e]en") == []  # glob characters match literally


def test_card_shows_channels_an_interrupted_scrape_and_files(tmp_path, monkeypatch):
    monkeypatch.setattr(scraped_files, "BASES_DIR", str(tmp_path))
    project = projects.add("Green", ["@a", "@b"])
    assert "Файлов скрапа пока нет." in projects.card(project)
    assert projects.label(project) == "📁 Green · каналов: 2"

    _write_parquet(tmp_path / "Green_posts_01.01.2024-31.01.2024.parquet", rows=3)
    _checkpoint(tmp_path, "Green", t_index=1240)
    card = projects.card(project)
    assert "Каналы (2): @a, @b" in card and "собрано постов 1240" in card
    assert "• posts · 01.01.2024-31.01.2024 · 3" in card
    assert projects.label(project) == "📁 Green · каналов: 2 ⏸"


def test_card_lists_at_most_max_card_files(tmp_path):
    project = projects.add("Green", ["@a"])
    for day in range(1, projects.MAX_CARD_FILES + 6):
        _write_parquet(tmp_path / f"Green_posts_{day:02}.01.2024-31.01.2024.parquet")
    card = projects.card(project)
    assert card.count("\n• posts · ") == projects.MAX_CARD_FILES and card.endswith("… и ещё 5")


def test_importable_offers_the_finished_scrapes_with_no_project_yet(tmp_path):
    _posts(tmp_path, "Old", 1_000, channels=["@o"])
    _posts(tmp_path, "Green", 1_500, channels=["@a"]).rename(tmp_path / "Green_posts_2023.parquet")  # older, same name
    _posts(tmp_path, "Green", 2_000, channels=["@a", "t.me/+inv"])
    _posts(tmp_path, "Mute", 3_000, groups=())       # no channels in the file: nothing to make it of
    _posts(tmp_path, "Taken", 4_000, channels=["@t"])
    projects.add("taken", ["@t"])
    assert projects.importable() == [("Green", ["@a", "t.me/+inv"]), ("Old", ["@o"])]  # newest first


# --- the bot ----------------------------------------------------------------------------------

def _router():
    from bot.routers import projects as router
    return router


def test_bot_with_no_projects_offers_only_a_new_one():
    router = _router()
    start = _SCallback()
    asyncio.run(router.projects_menu(start.message, _State()))
    text, markup = start.message.answers[0]
    assert text.startswith("📁 <b>Проекты</b> — пока ни одного") and _buttons(markup) == ["➕ Новый проект"]


def test_bot_creates_a_project_and_shows_its_card():
    from bot.states import Projects

    router = _router()
    state = _State()
    asyncio.run(router.project_pick(_SCallback(), ChoiceCB(scope="prj", value="new"), state))
    assert state.state == Projects.name

    bad = _Msg(text="a/b")
    asyncio.run(router.project_name(bad, state))
    assert "Не подходит" in bad.answers[0][0] and state.state == Projects.name
    asyncio.run(router.project_name(_Msg(text="Green"), state))
    assert state.state == Projects.channels

    empty = _Msg(text=" , ")
    asyncio.run(router.project_channels(empty, state))
    assert "ни одного канала" in empty.answers[0][0] and projects.load() == []

    done = _Msg(text="@a, t.me/b")
    asyncio.run(router.project_channels(done, state))
    assert projects.load() == [projects.Project("Green", ["@a", "t.me/b"])]
    text, markup = done.answers[0]
    assert text.startswith("📁 Green") and _buttons(markup) == [
        "▶️ Скрап проекта", "✏️ Каналы", "🏷 Переименовать", "🗑 Удалить", "⬅️ К проектам"]
    assert state.data == {"project_name": "Green", "project_names": []}


def test_bot_lists_projects_and_opens_one_by_its_button():
    router = _router()
    projects.add("A", ["@a"])
    projects.add("B", ["@b", "@c"])
    state = _State()
    start = _SCallback()
    asyncio.run(router.projects_menu(start.message, state))
    assert _buttons(start.message.answers[0][1]) == ["📁 A · каналов: 1", "📁 B · каналов: 2", "➕ Новый проект"]

    pick = _SCallback()
    asyncio.run(router.project_pick(pick, ChoiceCB(scope="prj", value="1"), state))
    assert pick.message.answers[0][0].startswith("📁 B")


def test_bot_list_buttons_still_work_after_a_card_is_opened():
    router = _router()
    projects.add("A", ["@a"])
    projects.add("B", ["@b"])
    state = _State()
    asyncio.run(router.projects_menu(_SMsg(), state))
    asyncio.run(router.project_pick(_SCallback(), ChoiceCB(scope="prj", value="0"), state))
    again = _SCallback()  # the list's message above, its other button
    asyncio.run(router.project_pick(again, ChoiceCB(scope="prj", value="1"), state))
    assert again.message.answers[0][0].startswith("📁 B")


def test_bot_makes_a_project_from_a_finished_scrape(tmp_path):
    router = _router()
    _posts(tmp_path, "Green", 1_000, channels=["@a", "@b"])
    state = _State()
    start = _SCallback()
    asyncio.run(router.projects_menu(start.message, state))
    assert _buttons(start.message.answers[0][1]) == ["➕ Новый проект", "📥 Из готового скрапа"]

    offer = _SCallback()
    asyncio.run(router.project_pick(offer, ChoiceCB(scope="prj", value="import"), state))
    assert _buttons(offer.message.answers[0][1]) == ["📁 Green · каналов: 2"]

    pick = _SCallback()
    asyncio.run(router.project_import(pick, ChoiceCB(scope="prj_imp", value="0"), state))
    assert projects.load() == [projects.Project("Green", ["@a", "@b"])]
    card = pick.message.answers[0][0]
    assert card.startswith("📁 Green") and "• posts · 01.01.2024-31.01.2024 · 1" in card

    nothing = _SCallback()  # Green has its project now
    asyncio.run(router.project_pick(nothing, ChoiceCB(scope="prj", value="import"), state))
    assert nothing.message.answers[0][0].startswith("Не из чего сделать проект")


def test_bot_edits_channels_and_deletes_a_project_keeping_its_files(tmp_path, monkeypatch):
    router = _router()
    monkeypatch.setattr(scraped_files, "BASES_DIR", str(tmp_path))
    posts = tmp_path / "A_posts_01.01.2024-31.01.2024.parquet"
    _write_parquet(posts)
    projects.add("A", ["@a"])
    state = _State({"project_name": "A"})

    asyncio.run(router.project_action(_SCallback(), ChoiceCB(scope="prj_act", value="channels"), state,
                                      _pool(), None, None))
    edited = _Msg(text="@x @y")
    asyncio.run(router.project_edit(edited, state))
    assert projects.load()[0].channels == ["@x", "@y"] and "Каналы (2): @x, @y" in edited.answers[0][0]

    asyncio.run(router.project_action(_SCallback(), ChoiceCB(scope="prj_act", value="delete"), state,
                                      _pool(), None, None))
    keep = _SCallback()
    asyncio.run(router.project_delete(keep, ChoiceCB(scope="prj_del", value="no"), state))
    assert projects.load() and keep.message.answers[0][0].startswith("📁 A")

    confirm = _SCallback()
    asyncio.run(router.project_action(confirm, ChoiceCB(scope="prj_act", value="delete"), state,
                                      _pool(), None, None))
    gone = _SCallback()
    asyncio.run(router.project_delete(gone, ChoiceCB(scope="prj_del", value=_delete_value(confirm)), state))
    assert projects.load() == [] and posts.exists()
    assert "удалён, файлы скрапа на месте" in gone.message.answers[0][0]


def test_bot_stale_card_button_is_reported():
    router = _router()
    stale = _SCallback()
    asyncio.run(router.project_action(stale, ChoiceCB(scope="prj_act", value="scrape"), _State(),
                                      _pool(), None, None))
    assert stale.message.answers[0][0].startswith("Флоу устарел")


def _delete_value(confirm):
    """The «🗑 Удалить» button's value on the confirmation `confirm` was answered with."""
    return ChoiceCB.unpack(confirm.message.answers[-1][1].inline_keyboard[0][0].callback_data).value


def test_bot_an_old_delete_confirmation_does_not_delete_the_project_opened_since():
    router = _router()
    projects.add("A", ["@a"])
    projects.add("B", ["@b"])
    state = _State()
    asyncio.run(router.projects_menu(_SMsg(), state))
    asyncio.run(router.project_pick(_SCallback(), ChoiceCB(scope="prj", value="0"), state))  # A's card
    confirm_a = _SCallback()
    asyncio.run(router.project_action(confirm_a, ChoiceCB(scope="prj_act", value="delete"), state,
                                      _pool(), None, None))  # «Удалить проект «A»?»
    asyncio.run(router.project_pick(_SCallback(), ChoiceCB(scope="prj", value="1"), state))  # B's card

    old = _SCallback()  # 🗑 on A's confirmation above
    asyncio.run(router.project_delete(old, ChoiceCB(scope="prj_del", value=_delete_value(confirm_a)), state))
    assert [p.name for p in projects.load()] == ["A", "B"]
    assert old.message.answers[0][0].startswith("Флоу устарел")

    # B's own confirmation shown meanwhile: still not A's button that deletes B
    asyncio.run(router.project_action(_SCallback(), ChoiceCB(scope="prj_act", value="delete"), state,
                                      _pool(), None, None))  # «Удалить проект «B»?»
    old = _SCallback()
    asyncio.run(router.project_delete(old, ChoiceCB(scope="prj_del", value=_delete_value(confirm_a)), state))
    assert [p.name for p in projects.load()] == ["A", "B"]
    assert old.message.answers[0][0].startswith("Флоу устарел")


def test_bot_reports_an_unreadable_file():
    router = _router()
    with open(projects.PATH, "w", encoding="utf-8") as file:
        file.write("{broken")
    start = _SCallback()
    asyncio.run(router.projects_menu(start.message, _State()))
    assert "не читается" in start.message.answers[0][0]


def test_bot_project_scrape_skips_name_and_channels_and_runs_on_them(monkeypatch, tmp_path):
    from bot.routers import scraping
    from bot.services.jobs import JobManager
    from bot.states import Scrape

    router = _router()
    monkeypatch.setattr(scraping, "DEFAULT_OUT", str(tmp_path))
    monkeypatch.setattr(scraped_files, "BASES_DIR", str(tmp_path))
    calls = _scrapes(monkeypatch)
    projects.add("Green", ["@a", "t.me/b"])
    state = _State({"project_name": "Green"})

    go = _SCallback()
    asyncio.run(router.project_action(go, ChoiceCB(scope="prj_act", value="scrape"), state, _pool(), None,
                                      JobManager()))
    # one account: taken at once; the channels as if typed and named (no lookup here) -> the dates
    assert state.state == Scrape.date_min and go.message.answers[-1][0].startswith("Дата с")
    assert state.data["name"] == "Green" and state.data["out_dir"] == str(tmp_path)

    asyncio.run(scraping.scrape_dmin(_Msg(text="01.01.2024"), state))
    asyncio.run(scraping.scrape_run(_Msg(text="31.01.2024"), state))
    asyncio.run(scraping.scrape_go(_SCallback(), ChoiceCB(scope="scrape_go", value="run"), state, _pool(), None,
                                   JobManager()))
    [params] = calls
    assert (params.name, params.channels, params.resume) == ("Green", ["@a", "t.me/b"], False)


def test_bot_project_scrape_offers_to_continue_and_fresh_keeps_the_channels(monkeypatch, tmp_path):
    from bot.routers import scraping
    from bot.services.jobs import JobManager
    from bot.states import Scrape

    router = _router()
    monkeypatch.setattr(scraping, "DEFAULT_OUT", str(tmp_path))
    monkeypatch.setattr(scraped_files, "BASES_DIR", str(tmp_path))
    _checkpoint(tmp_path, "Green")
    projects.add("Green", ["@new"])
    state = _State({"project_name": "Green"})

    go = _SCallback()
    asyncio.run(router.project_action(go, ChoiceCB(scope="prj_act", value="scrape"), state, _pool(), None,
                                      JobManager()))
    assert state.state == Scrape.resume and "незавершённый скрап «Green»" in go.message.answers[0][0]

    fresh = _SCallback()
    asyncio.run(scraping.scrape_resume(fresh, ChoiceCB(scope="scrape_resume", value="fresh"), state, _pool(),
                                       None, JobManager()))
    assert state.state == Scrape.date_min and state.data["channels"] == "@new"  # not asked again


def test_ad_hoc_scrape_flow_is_unchanged(monkeypatch, tmp_path):
    from bot.routers import scraping
    from bot.services.jobs import JobManager
    from bot.states import Scrape

    monkeypatch.setattr(scraping, "DEFAULT_OUT", str(tmp_path))
    _checkpoint(tmp_path, "n")
    state = _State()
    asyncio.run(scraping.scrape_name(_SMsg(text="n"), state, _pool(), None, JobManager()))
    fresh = _SCallback()
    asyncio.run(scraping.scrape_resume(fresh, ChoiceCB(scope="scrape_resume", value="fresh"), state, _pool(),
                                       None, JobManager()))
    assert state.state == Scrape.channels and fresh.message.answers[0][0].startswith("Каналы/группы")


# --- the CLI ----------------------------------------------------------------------------------

def test_cli_menu_discovers_only_the_projects_function():
    from functions import projects as cli
    from bot.services.registry import BOT_FUNCTIONS_BY_KEY

    # the discovery takes every *Func class in the module: ScrapeFunc must not be one twice
    assert [name for name, _ in inspect.getmembers(cli, inspect.isclass) if name.endswith("Func")] == ["ProjectsFunc"]
    assert BOT_FUNCTIONS_BY_KEY["projects"].classname == "ProjectsFunc"


def test_cli_creates_a_project_and_scrapes_it_on_its_channels(monkeypatch, tmp_path):
    from functions import projects as cli
    from functions import scraper

    monkeypatch.setattr(scraped_files, "BASES_DIR", str(tmp_path))
    captured = {}
    monkeypatch.setattr(scraper, "pick_session", lambda storage, personal=None: _PICKED)
    monkeypatch.setattr(scraper, "scrape_run", lambda creds, params: captured.update(params=params))
    monkeypatch.setattr(cli.TelethonFunction, "ask_int", staticmethod(lambda *a, **k: 1))  # [1] ➕ Новый
    _answers(monkeypatch, cli, texts=["Green", "@a, @b", ""])  # name, channels, no action on the card
    cli.ProjectsFunc(FakeStorage(), FakeSettings()).execute()
    assert projects.load() == [projects.Project("Green", ["@a", "@b"])]

    # one rich Prompt class for both modules: [1] Green -> ▶️ Скрап проекта, then the scrape's own
    # questions — the dates and the keyword, no name and no channels (the limit: ask_int's 1)
    _answers(monkeypatch, cli, texts=["1", "01.01.2024", "31.01.2024", ""], bools=[True, True, True])
    cli.ProjectsFunc(FakeStorage(), FakeSettings()).execute()
    p = captured["params"]
    assert (p.name, p.channels, p.max_messages) == ("Green", ["@a", "@b"], 1)


def test_cli_makes_a_project_from_a_finished_scrape(monkeypatch, tmp_path):
    from functions import projects as cli

    _posts(tmp_path, "Green", 1_000, channels=["@a"])
    numbers = iter([2, 1])  # [2] 📥 Из готового скрапа -> [1] Green
    monkeypatch.setattr(cli.TelethonFunction, "ask_int", staticmethod(lambda *a, **k: next(numbers)))
    _answers(monkeypatch, cli, texts=[""])  # no action on the card
    cli.ProjectsFunc(FakeStorage(), FakeSettings()).execute()
    assert projects.load() == [projects.Project("Green", ["@a"])]


def test_cli_edits_and_deletes_a_project(monkeypatch):
    from functions import projects as cli

    projects.add("Green", ["@a"])
    monkeypatch.setattr(cli.TelethonFunction, "ask_int", staticmethod(lambda *a, **k: 1))
    _answers(monkeypatch, cli, texts=["2", "@x"])
    cli.ProjectsFunc(FakeStorage(), FakeSettings()).execute()
    assert projects.load()[0].channels == ["@x"]

    _answers(monkeypatch, cli, texts=["3"], bools=[True])
    cli.ProjectsFunc(FakeStorage(), FakeSettings()).execute()
    assert projects.load() == []


def _cli_card(monkeypatch, texts):
    """Open the first project's card in the CLI with scripted answers; (execute's result, printed)."""
    from functions import projects as cli

    printed = []
    monkeypatch.setattr(cli.TelethonFunction, "ask_int", staticmethod(lambda *a, **k: 1))
    monkeypatch.setattr(cli.console, "print", lambda text="", *a, **k: printed.append(str(text)))
    _answers(monkeypatch, cli, texts=texts)
    settings = type("Settings", (FakeSettings,), {"delay": [1, 2]})()  # the contacts' delay default
    return cli.ProjectsFunc(FakeStorage(), settings).execute(), printed


def test_cli_card_offers_the_work_on_the_data_it_has(monkeypatch, tmp_path):
    projects.add("Green", ["@a"])
    _, printed = _cli_card(monkeypatch, [""])
    assert not any("[6]" in p or "[8]" in p for p in printed)  # no files yet

    _write_parquet(tmp_path / "Green_participants_01.01.2024-31.01.2024.parquet")
    _, printed = _cli_card(monkeypatch, [""])
    assert any("[6] 📨 Рассылка по базе  [7] 📇 В контакты" in p for p in printed)
    assert not any("[8]" in p for p in printed)

    _posts(tmp_path, "Green", 1_000)
    _, printed = _cli_card(monkeypatch, [""])
    assert any("[8] ✅ Верификация  [9] 📈 Анализ" in p for p in printed)


def test_cli_card_mailing_and_contacts_offer_the_project_s_bases_only(monkeypatch, tmp_path):
    from functions import add_contacts, pmmailing

    projects.add("Green", ["@a"])
    green = tmp_path / "Green_participants_01.01.2024-31.01.2024.parquet"
    _write_parquet(green)
    _write_parquet(tmp_path / "Other_participants_01.01.2024-31.01.2024.parquet")
    offered = []

    def ask_file(label, files, default=""):
        offered.append([path for path, _ in files])
        return str(tmp_path / "missing.parquet")  # the function then stops: no such file

    for module, cls, action in ((pmmailing, "PmMailingFunc", "6"), (add_contacts, "AddContactsFunc", "7")):
        monkeypatch.setattr(getattr(module, cls), "ask_file", staticmethod(ask_file))
        monkeypatch.setattr(getattr(module, cls), "run", lambda *a, **k: asyncio.sleep(0))
        coro, _ = _cli_card(monkeypatch, [action, "1-2"])  # the action, then contacts' delay
        assert inspect.isawaitable(coro)  # run_instance awaits it on the sessions' loop
        asyncio.run(coro)
    assert offered == [[str(green)], [str(green)]]


def test_cli_card_verify_and_analysis_offer_the_project_s_posts_only(monkeypatch, tmp_path):
    from functions import scraper, scraper_analysis

    projects.add("Green", ["@a"])
    green = _posts(tmp_path, "Green", 1_000)
    _posts(tmp_path, "Other", 2_000)
    _write_parquet(tmp_path / "Green_reactors_01.01.2024-31.01.2024.parquet")
    offered = []

    class _Stop(Exception):
        pass

    def ask_file(label, files, default=""):
        offered.append([path for path, _ in files])
        raise _Stop

    monkeypatch.setattr(scraper, "pick_session", lambda *a: _PICKED)
    monkeypatch.setattr(scraper.VerifyFunc, "ask_file", staticmethod(ask_file))
    with pytest.raises(_Stop):
        _cli_card(monkeypatch, ["8"])
    assert offered == [[str(green)]]

    # 📈 Анализ → 🧩 Объединить ([4]), blank: all the project's posts files, as the bot's button
    combined = []
    monkeypatch.setattr(scraper_analysis.analysis, "combine", lambda inp, out, dedup: combined.append(inp))
    _cli_card(monkeypatch, ["9", "4", "", "out", "Group,Message ID"])
    assert combined == [[str(green)]]


def test_cli_combine_takes_the_posts_files_by_default(monkeypatch, tmp_path):
    from functions import scraper_analysis
    from scraper.datafiles import resolve_inputs

    green = _posts(tmp_path, "Green", 1_000)
    _write_parquet(tmp_path / "Green_participants_01.01.2024-31.01.2024.parquet")
    _write_parquet(tmp_path / "Green_reactors_01.01.2024-31.01.2024.parquet")
    combined = []
    monkeypatch.setattr(scraper_analysis.analysis, "combine", lambda inp, out, dedup: combined.append(inp))
    monkeypatch.setattr(scraper_analysis.Prompt, "ask", lambda *a, default=None, **k: default)  # Enter each time

    scraper_analysis._combine(None)
    assert resolve_inputs(combined[0]) == [green]  # the bases are not even read


# --- the top-up (🔄 Дособрать) ------------------------------------------------------------------

def _scraped(folder, name, mtime=1_000, window=("2024-01-01", "2024-01-31"), channels=("@a",), keyword=None):
    """A finished scrape's posts file with what the scrape stores in it."""
    df = pd.DataFrame({"Group": ["@a"], "Message ID": ["1"]})
    if window:
        df.attrs["scrape_window"] = {"date_min": window[0], "date_max": window[1]}
    if channels:
        df.attrs["channels"] = list(channels)
    if keyword:
        df.attrs["keyword"] = keyword
    path = folder / f"{name}_posts_01.01.2024-31.01.2024.parquet"
    df.to_parquet(path)
    os.utime(path, (mtime, mtime))
    return path


def _today():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).date()


def test_topup_continues_the_newest_scrape_from_a_week_before_its_end(tmp_path):
    from datetime import date

    project = projects.add("Green", ["@a"])
    assert projects.topup(project) is None  # nothing scraped yet
    _scraped(tmp_path, "Green", window=None)
    assert projects.topup(project) is None  # an older file: no window to continue from

    path = _scraped(tmp_path, "Green", channels=("@a", "@b"), keyword="war")
    plan = projects.topup(project)
    assert (plan.posts, plan.channels, plan.keyword) == (str(path), ["@a", "@b"], "war")
    assert (plan.date_min, plan.date_max) == (date(2024, 1, 24), _today())


def test_card_offers_the_top_up_and_says_when_its_channels_differ(tmp_path):
    project = projects.add("Green", ["@a"])
    _scraped(tmp_path, "Green", channels=("@old",))
    card = projects.card(project)
    assert f"🔄 Дособор: с 24.01.2024 по {_today():%d.%m.%Y}" in card
    assert "Каналы дособора — как в прошлом скрапе: @old" in card

    _checkpoint(tmp_path, "Green")  # unfinished: continuing it comes first
    assert "🔄" not in projects.card(project)


def test_bot_top_up_goes_from_the_card_to_the_summary_and_runs_on_the_files_data(monkeypatch, tmp_path):
    from bot.routers import scraping
    from bot.services.jobs import JobManager
    from bot.states import Scrape

    router = _router()
    monkeypatch.setattr(scraping, "DEFAULT_OUT", str(tmp_path))
    calls = _scrapes(monkeypatch)
    project = projects.add("Green", ["@new"])
    path = _scraped(tmp_path, "Green", channels=("@a",), keyword="war")
    state = _State()

    card = _SCallback()
    asyncio.run(router._show_card(card.message, state, project))
    assert "🔄 Дособрать" in _buttons(card.message.answers[0][1])

    go = _SCallback()
    asyncio.run(router.project_action(go, ChoiceCB(scope="prj_act", value="topup"), state, _pool(), None,
                                      JobManager()))
    text, markup = go.message.answers[-1]
    assert state.state == Scrape.confirm and text.startswith("Каналы: @a\nПериод: 24.01.2024")
    assert f"Дособор к {path.name}" in text and _buttons(markup) == ["▶️ Запустить", "⚙️ Дополнительно"]

    more = _SCallback()
    asyncio.run(scraping.scrape_go(more, ChoiceCB(scope="scrape_go", value="more"), state, _pool(), None,
                                   JobManager()))
    assert not any(b.startswith("🔎") for b in _buttons(more.message.answers[0][1]))  # the keyword is fixed

    asyncio.run(scraping.scrape_option(_SCallback(), ChoiceCB(scope="scrape_opt", value="run"), state,
                                       _pool(), None, JobManager()))
    [params] = calls
    assert (params.name, params.channels, params.keyword, params.extend) == ("Green", ["@a"], "war", str(path))
    assert (params.date_min.date().isoformat(), params.date_max.date()) == ("2024-01-24", _today())


def test_bot_offers_no_top_up_while_a_scrape_is_unfinished(tmp_path):
    router = _router()
    project = projects.add("Green", ["@a"])
    _scraped(tmp_path, "Green")
    _checkpoint(tmp_path, "Green")
    card = _SCallback()
    asyncio.run(router._show_card(card.message, _State(), project))
    assert "🔄 Дособрать" not in _buttons(card.message.answers[0][1])


def test_cli_top_up_runs_on_the_files_data(monkeypatch, tmp_path):
    from functions import projects as cli
    from functions import scraper

    captured = {}
    monkeypatch.setattr(scraper, "pick_session", lambda storage, personal=None: _PICKED)
    monkeypatch.setattr(scraper, "scrape_run", lambda creds, params: captured.update(params=params))
    projects.add("Green", ["@a"])
    path = _scraped(tmp_path, "Green", channels=("@a", "@b"))
    monkeypatch.setattr(cli.TelethonFunction, "ask_int", staticmethod(lambda *a, **k: 1))  # [1] Green
    _answers(monkeypatch, cli, texts=["4"], bools=[True, False, True])  # 🔄; comments, no reactors, base
    cli.ProjectsFunc(FakeStorage(), FakeSettings()).execute()

    p = captured["params"]
    assert (p.name, p.channels, p.extend, p.with_reactors) == ("Green", ["@a", "@b"], str(path), False)
    assert p.max_messages == 1  # asked, as the bot's «🔢 Максимум постов» (ask_int's 1)
    assert p.date_min.date().isoformat() == "2024-01-24"


# --- the card runs the sections' flows on the project's files only ------------------------------

def _pool_of(*paths):
    """A worker pool with these session paths (none: no workers)."""
    import types

    from bot.services.delegation import WorkerPool

    clients = [object() for _ in paths]
    by_client = dict(zip(map(id, clients), paths))
    return WorkerPool(types.SimpleNamespace(sessions=clients, jsessions_paths={},
                                            get_session_path=lambda c: by_client[id(c)]))


def _scraped_files(tmp_path):
    """Green has a base and a posts file; Other's files sit in the same folder."""
    for stem in ("Green_participants_01.01.2024-31.01.2024", "Green_posts_01.01.2024-31.01.2024",
                 "Other_participants_01.01.2024-31.01.2024", "Other_posts_01.01.2024-31.01.2024"):
        _write_parquet(tmp_path / f"{stem}.parquet")


def _two_projects(tmp_path):
    _scraped_files(tmp_path)
    return projects.add("Green", ["@a"])


def _card_action(action, state, pool=None):
    from bot.services.jobs import JobManager

    callback = _SCallback()
    asyncio.run(_router().project_action(callback, ChoiceCB(scope="prj_act", value=action), state,
                                         pool or _pool(), None, JobManager()))
    return callback


def test_card_offers_the_work_on_the_data_it_has(tmp_path):
    router = _router()
    project = projects.add("Green", ["@a"])
    card = _SCallback()
    asyncio.run(router._show_card(card.message, _State(), project))
    assert _buttons(card.message.answers[0][1]) == ["▶️ Скрап проекта", "✏️ Каналы", "🏷 Переименовать", "🗑 Удалить", "⬅️ К проектам"]

    _scraped_files(tmp_path)
    card = _SCallback()
    asyncio.run(router._show_card(card.message, _State(), project))
    assert _buttons(card.message.answers[0][1])[1:5] == ["📨 Рассылка по базе", "📇 В контакты",
                                                          "✅ Верификация", "📈 Анализ"]


def test_card_mailing_offers_the_project_s_bases_only(tmp_path):
    from bot.routers import broadcasts
    from bot.states import PmMailing

    _two_projects(tmp_path)
    state = _State({"project_name": "Green"})
    callback = _card_action("mail", state)
    text, markup = callback.message.answers[0]
    assert _buttons(markup) == ["📄 participants · 01.01.2024-31.01.2024 · 2"] and state.state == PmMailing.path

    pick = _SCallback()
    asyncio.run(broadcasts.mail_base(pick, ChoiceCB(scope="mail_base", value="0"), state))
    assert state.data["path"] == str(tmp_path / "Green_participants_01.01.2024-31.01.2024.parquet")
    assert pick.message.answers[0][0] == "Пропустить тех, кому уже писали раньше?"  # the mailing goes on as usual


def test_card_contacts_offer_the_project_s_bases_only_and_need_workers(tmp_path, monkeypatch):
    from bot.routers import audience

    _two_projects(tmp_path)
    no_workers = _card_action("contacts", _State({"project_name": "Green"}), pool=_pool_of())
    assert no_workers.message.answers[0][0].startswith("Нет воркер-аккаунтов")

    launched = []

    async def launch(message, path, *rest):
        launched.append(path)

    monkeypatch.setattr(audience, "_launch", launch)
    state = _State({"project_name": "Green"})
    callback = _card_action("contacts", state)
    assert _buttons(callback.message.answers[0][1]) == ["📄 participants · 01.01.2024-31.01.2024 · 2"]
    asyncio.run(audience.run_base(_SCallback(), ChoiceCB(scope="contacts_base", value="0"), state, _pool(),
                                  {}, None, None))
    assert launched == [str(tmp_path / "Green_participants_01.01.2024-31.01.2024.parquet")]


def test_card_verify_offers_the_project_s_posts_only(tmp_path):
    _two_projects(tmp_path)
    callback = _card_action("verify", _State({"project_name": "Green"}))  # one account: taken at once
    assert _buttons(callback.message.answers[-1][1]) == ["📄 posts · 01.01.2024-31.01.2024 · 2"]


def test_card_analysis_offers_the_project_s_files_only(tmp_path):
    from bot.routers import scraping

    _two_projects(tmp_path)
    state = _State({"project_name": "Green"})
    _card_action("analysis", state)
    links = _SCallback()
    asyncio.run(scraping.analysis_tool(links, ChoiceCB(scope="an_tool", value="links"), state))
    assert _buttons(links.message.answers[0][1]) == ["📄 posts · 01.01.2024-31.01.2024 · 2"]
    read = _SCallback()
    asyncio.run(scraping.analysis_tool(read, ChoiceCB(scope="an_tool", value="read"), state))
    assert sorted(_buttons(read.message.answers[0][1])) == ["📄 participants · 01.01.2024-31.01.2024 · 2",
                                                            "📄 posts · 01.01.2024-31.01.2024 · 2"]

    plain = _SCallback()  # the section's own analysis: every file again
    asyncio.run(scraping.analysis_start(plain, state))
    asyncio.run(scraping.analysis_tool(plain, ChoiceCB(scope="an_tool", value="links"), state))
    assert len(_buttons(plain.message.answers[-1][1])) == 2


def test_project_files_by_kind(tmp_path):
    _two_projects(tmp_path)
    assert [label.split(" · ")[0] for _, label in scraped_files.project_files("Green", kinds=("posts",))] == ["posts"]
    assert len(scraped_files.project_files("Green")) == 2


# --- renaming a project with its files ---------------------------------------------------------------

SPAN = "01.01.2024-31.01.2024"
GREEN = [f"Green_posts_{SPAN}.parquet", f"Green_reactors_{SPAN}.parquet", f"Green_participants_{SPAN}.parquet",
         "Green_participants_members.parquet", f"Green_posts_{SPAN}_missed.parquet",
         f"Green_links_{SPAN}.xlsx", f"Green_summary_{SPAN}_monthly.xlsx", "Green_sample.xlsx"]
NOT_GREEN = [f"Green_x_posts_{SPAN}.parquet",        # the project Green_x's
             f"Green_links_posts_{SPAN}.parquet",    # a one-off scrape named Green_links
             "Green_notes.txt", "Greenish_posts.parquet"]


def _green_on_disk(tmp_path):
    for name in GREEN + NOT_GREEN:
        (tmp_path / name).write_bytes(b"x")
    ckpt = tmp_path / "Green_partial" / "checkpoint"
    ckpt.mkdir(parents=True)
    (ckpt / "resume.json").write_text(json.dumps({"name": "Green", "channels": ["@a"],
                                                  "extend": str(tmp_path / GREEN[0])}))
    (tmp_path / "Green_previous").mkdir()
    (tmp_path / "Green_previous" / GREEN[0]).write_bytes(b"x")
    projects.add("Green", ["@a"])
    projects.add("Green_x", ["@b"])


def _bases(tmp_path):
    """The bases folder's own entries (the test's projects file and bot marker sit there too)."""
    return sorted(p.name for p in tmp_path.iterdir() if not p.name.endswith((".json", ".bak")))


def test_project_entries_are_its_files_and_folders_only(tmp_path):
    _green_on_disk(tmp_path)
    assert sorted(p.name for p in scraped_files.project_entries("Green")) == sorted(
        GREEN + ["Green_partial", "Green_previous"])


def test_rename_moves_everything_of_the_project_and_its_checkpoint(tmp_path):
    _green_on_disk(tmp_path)
    project, moved = projects.rename("Green", " Blue ")

    assert (project.name, moved) == ("Blue", len(GREEN) + 2)
    assert all((tmp_path / name.replace("Green", "Blue", 1)).exists() for name in GREEN)
    assert all((tmp_path / name).exists() for name in NOT_GREEN)  # nobody else's file taken
    assert (tmp_path / "Blue_previous" / GREEN[0]).exists()       # a folder's own files keep their names
    meta = json.loads((tmp_path / "Blue_partial" / "checkpoint" / "resume.json").read_text())
    assert meta["name"] == "Blue" and meta["extend"] == str(tmp_path / f"Blue_posts_{SPAN}.parquet")
    assert projects.unfinished("Blue")["name"] == "Blue" and projects.unfinished("Green") is None  # continued as Blue
    assert [p.name for p in projects.load()] == ["Blue", "Green_x"]
    assert [p["name"] for p in json.loads(open(projects.PATH + ".bak").read())] == ["Green", "Green_x"]


def test_rename_refuses_and_changes_nothing(tmp_path):
    _green_on_disk(tmp_path)
    before = _bases(tmp_path)
    with pytest.raises(ValueError, match="уже есть"):  # another project's name
        projects.rename("Green", "green_x")
    with pytest.raises(ValueError, match="не изменилось"):
        projects.rename("Green", "Green")
    (tmp_path / f"Blue_links_{SPAN}.xlsx").write_bytes(b"other")  # the new name is taken on disk
    with pytest.raises(ValueError, match=f"на диске уже есть Blue_links_{SPAN}.xlsx"):
        projects.rename("Green", "Blue")
    (tmp_path / f"Blue_links_{SPAN}.xlsx").unlink()
    json_file.save(scraped_files.SCRAPE_MARKER, {"name": "Green", "out_dir": str(tmp_path)})  # the bot scrapes it
    with pytest.raises(ValueError, match="скрап бота"):
        projects.rename("Green", "Blue")
    assert _bases(tmp_path) == before
    assert [p.name for p in projects.load()] == ["Green", "Green_x"]


def test_rename_undoes_a_move_that_fails_midway(tmp_path, monkeypatch):
    import os

    _green_on_disk(tmp_path)
    before = _bases(tmp_path)
    real, calls = os.replace, []

    def flaky(source, target):
        calls.append(source)
        if len(calls) == 3:
            raise OSError("disk full")
        real(source, target)

    monkeypatch.setattr(projects.os, "replace", flaky)
    with pytest.raises(ValueError, match="всё возвращено как было"):
        projects.rename("Green", "Blue")
    assert _bases(tmp_path) == before
    assert projects.load()[0].name == "Green"


def test_rename_may_change_the_case_only(tmp_path):
    _green_on_disk(tmp_path)
    assert projects.rename("Green", "GREEN")[0].name == "GREEN"
    assert (tmp_path / f"GREEN_posts_{SPAN}.parquet").exists()


def test_bot_renames_a_project_from_its_card(tmp_path):
    from bot.states import Projects

    router = _router()
    _green_on_disk(tmp_path)
    state = _State({"project_name": "Green", "project_names": ["Green", "Green_x"]})
    _card_action("rename", state)
    assert state.state == Projects.rename

    bad = _Msg(text="Green_x")
    asyncio.run(router.project_rename(bad, state))
    assert "Не подходит: проект с таким именем уже есть" in bad.answers[0][0] and state.state == Projects.rename

    done = _Msg(text="Blue")
    asyncio.run(router.project_rename(done, state))
    assert done.answers[0][0] == f"✅ Проект «Green» теперь «Blue». Переименовано файлов и папок: {len(GREEN) + 2}."
    assert done.answers[1][0].startswith("📁 Blue")
    assert state.data["project_names"] == ["Blue", "Green_x"]  # the list's buttons follow the rename


def test_cli_renames_a_project(monkeypatch, tmp_path):
    from functions import projects as cli

    _green_on_disk(tmp_path)
    monkeypatch.setattr(cli.TelethonFunction, "ask_int", staticmethod(lambda *a, **k: 1))  # [1] Green
    _answers(monkeypatch, cli, texts=["5", "Blue"])
    cli.ProjectsFunc(FakeStorage(), FakeSettings()).execute()
    assert [p.name for p in projects.load()] == ["Blue", "Green_x"]
    assert (tmp_path / f"Blue_posts_{SPAN}.parquet").exists()


def test_every_analysis_output_is_renamed_with_its_project():
    """The analysis names its files with output_path(<posts>, "<suffix>"): a suffix project_entries
    doesn't know would stay behind with the old name."""
    import re

    suffixes = set()
    for path in ("bot/routers/scraping.py", "functions/scraper_analysis.py"):
        suffixes |= set(re.findall(r'output_path\(\w+, "(\w+)"\)', open(path, encoding="utf-8").read()))
    assert suffixes and suffixes <= set(scraped_files.ANALYSIS_SUFFIXES) | {"participants"}
