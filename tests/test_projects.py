"""Projects (modules/projects.py): the file, its backups and names, a project's files, and the
bot's and the CLI's projects screens on the same model, a project's scrape included."""

import asyncio
import inspect
import json
import os

import pandas as pd
import pytest

from bot.callbacks import ChoiceCB
from modules import projects, scraped_files
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
    assert projects.label(project) == "Green · каналов: 2"

    _write_parquet(tmp_path / "Green_posts_01.01.2024-31.01.2024.parquet", rows=3)
    _checkpoint(tmp_path, "Green", t_index=1240)
    card = projects.card(project)
    assert "Каналы (2): @a, @b" in card and "собрано постов 1240" in card
    assert "• posts · 01.01.2024-31.01.2024 · 3" in card
    assert projects.label(project) == "Green · каналов: 2 ⏸"


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
    asyncio.run(router.projects_start(start, _State()))
    text, markup = start.message.answers[0]
    assert text.startswith("Проектов пока нет") and _buttons(markup) == ["➕ Новый проект"]


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
        "▶️ Скрап проекта", "✏️ Каналы", "🗑 Удалить", "⬅️ К проектам"]
    assert state.data == {"project_name": "Green", "project_names": []}


def test_bot_lists_projects_and_opens_one_by_its_button():
    router = _router()
    projects.add("A", ["@a"])
    projects.add("B", ["@b", "@c"])
    state = _State()
    start = _SCallback()
    asyncio.run(router.projects_start(start, state))
    assert _buttons(start.message.answers[0][1]) == ["A · каналов: 1", "B · каналов: 2", "➕ Новый проект"]

    pick = _SCallback()
    asyncio.run(router.project_pick(pick, ChoiceCB(scope="prj", value="1"), state))
    assert pick.message.answers[0][0].startswith("📁 B")


def test_bot_list_buttons_still_work_after_a_card_is_opened():
    router = _router()
    projects.add("A", ["@a"])
    projects.add("B", ["@b"])
    state = _State()
    asyncio.run(router.projects_start(_SCallback(), state))
    asyncio.run(router.project_pick(_SCallback(), ChoiceCB(scope="prj", value="0"), state))
    again = _SCallback()  # the list's message above, its other button
    asyncio.run(router.project_pick(again, ChoiceCB(scope="prj", value="1"), state))
    assert again.message.answers[0][0].startswith("📁 B")


def test_bot_makes_a_project_from_a_finished_scrape(tmp_path):
    router = _router()
    _posts(tmp_path, "Green", 1_000, channels=["@a", "@b"])
    state = _State()
    start = _SCallback()
    asyncio.run(router.projects_start(start, state))
    assert _buttons(start.message.answers[0][1]) == ["➕ Новый проект", "📥 Из готового скрапа"]

    offer = _SCallback()
    asyncio.run(router.project_pick(offer, ChoiceCB(scope="prj", value="import"), state))
    assert _buttons(offer.message.answers[0][1]) == ["Green · каналов: 2"]

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

    gone = _SCallback()
    asyncio.run(router.project_delete(gone, ChoiceCB(scope="prj_del", value="yes"), state))
    assert projects.load() == [] and posts.exists()
    assert "удалён, файлы скрапа на месте" in gone.message.answers[0][0]


def test_bot_stale_card_button_is_reported():
    router = _router()
    stale = _SCallback()
    asyncio.run(router.project_action(stale, ChoiceCB(scope="prj_act", value="scrape"), _State(),
                                      _pool(), None, None))
    assert stale.message.answers[0][0].startswith("Флоу устарел")


def test_bot_reports_an_unreadable_file():
    router = _router()
    with open(projects.PATH, "w", encoding="utf-8") as file:
        file.write("{broken")
    start = _SCallback()
    asyncio.run(router.projects_start(start, _State()))
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
