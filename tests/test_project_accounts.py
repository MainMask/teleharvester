"""Each project's own workers and state (modules/projects.py): its folder, the migration from the
shared pool, an account moved between projects, and one account in one place only."""

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace as ns

import pytest

from modules import json_file, login, projects, restricted_workers, scraped_files, tdata_import
from test_worker_order import _write_jsession


def _project(name, channels=()):
    project = projects.add(name, list(channels))
    os.makedirs(projects.sessions_dir(project), exist_ok=True)
    return project


def test_a_new_project_gets_the_next_id_and_its_own_folder():
    green, blue = _project("Green"), _project("Blue")
    assert (green.id, blue.id) == (1, 2)
    assert projects.home(blue) == os.path.join(projects.PROJECTS_DIR, "2")
    assert projects.storage(blue, 1, "h").home == projects.home(blue)
    projects.rename("Blue", "Navy")
    assert projects.find("Navy").id == 2  # its folder stays: nothing of it on disk is moved


def test_migrate_gives_ids_and_each_old_project_a_copy_of_the_shared_stats():
    with open(projects.PATH, "w", encoding="utf-8") as file:  # from before projects had their own
        json.dump([{"name": "Green", "channels": ["@a"]}, {"name": "Blue", "channels": []}], file)
    pm, ledger = projects.LEGACY_STATS.values()
    json_file.save(pm, {"5": {"count": 1}})
    json_file.save(ledger, {"5": 77})
    json_file.save(scraped_files.SCRAPE_MARKER, {"name": "Blue", "out_dir": "x", "account": "a"})

    projects.migrate()

    green, blue = projects.load()
    assert (green.id, blue.id) == (1, 2)
    for project in (green, blue):
        assert json_file.load(os.path.join(projects.home(project), "pm_mailing.json"), None) == {"5": {"count": 1}}
        assert json_file.load(os.path.join(projects.home(project), "contacts.json"), None) == {"5": 77}
    assert scraped_files.scrape_markers() == {projects.home(blue): {"name": "Blue", "out_dir": "x", "account": "a"}}
    assert os.path.exists(pm) and os.path.exists(ledger)  # the shared ones stay as they were

    # once done, a no-op: a project made since starts without anyone's history
    red = _project("Red")
    json_file.save(os.path.join(projects.home(green), "pm_mailing.json"), {})
    projects.migrate()
    assert not os.path.exists(os.path.join(projects.home(red), "pm_mailing.json"))
    assert json_file.load(os.path.join(projects.home(green), "pm_mailing.json"), None) == {}


def test_a_moved_account_takes_its_restrictions_along(tmp_path):
    green, blue = _project("Green"), _project("Blue")
    _write_jsession(Path(projects.sessions_dir(green)), "111", "w1")
    old = os.path.join(projects.sessions_dir(green), "111.jsession")
    restricted_workers.save([old])
    restricted_workers.save_released([old])
    restricted_workers.save_status({old: "12 Nov 2026"})

    new = projects.move_account(old, projects.sessions_dir(blue))

    assert new == os.path.join(projects.sessions_dir(blue), "111.jsession") and os.path.exists(new)
    assert not os.path.exists(old)
    assert restricted_workers.load() == [new] and restricted_workers.load_released() == [new]
    assert restricted_workers.load_status() == {new: "12 Nov 2026"}
    assert projects.account_files(projects.sessions_dir(blue)) == [new]
    assert projects.place(new) == "в проекте «Blue»"


def test_an_account_with_a_scrape_to_continue_is_not_moved():
    green, blue = _project("Green"), _project("Blue")
    _write_jsession(Path(projects.sessions_dir(green)), "111", "w1")
    path = os.path.join(projects.sessions_dir(green), "111.jsession")
    json_file.save(scraped_files.SCRAPE_MARKER, {projects.home(green): {"account": path}})
    with pytest.raises(ValueError, match="незавершённый скрап"):
        projects.move_account(path, projects.sessions_dir(blue))
    assert os.path.exists(path)


def test_free_accounts_are_found_and_moved_into_a_project():
    os.makedirs(projects.FREE_DIR)
    _write_jsession(Path(projects.FREE_DIR), "222", "free1")
    path = os.path.join(projects.FREE_DIR, "222.jsession")
    assert projects.account_files(projects.FREE_DIR) == [path]
    assert projects.account_label(path) == "@free1 · +222"
    assert projects.place(path) == f"среди свободных ({projects.FREE_DIR}/)"
    assert projects.taken(path).endswith("возьмите его: «➕ Добавить» → «📦 Из свободных»")
    green = _project("Green")
    assert projects.find_account(phone="+222") == path
    moved = projects.move_account(path, projects.sessions_dir(green))
    assert projects.find_account(user_id=222) == moved


def test_a_project_with_workers_is_not_deleted():
    green = _project("Green")
    _write_jsession(Path(projects.sessions_dir(green)), "111", "w1")
    with pytest.raises(ValueError, match="аккаунтов: 1"):
        projects.remove("Green")
    projects.move_account(os.path.join(projects.sessions_dir(green), "111.jsession"), projects.FREE_DIR)
    projects.remove("Green")
    assert projects.load() == []


def test_another_project_s_worker_gets_no_code_in_this_one():
    green, blue = _project("Green"), _project("Blue")
    _write_jsession(Path(projects.sessions_dir(green)), "111", "w1")
    workers = projects.storage(blue, 1, "h")
    assert "уже воркер в проекте «Green»" in login.refusal("worker", "111", workers, None)
    assert "личным аккаунтом он не станет" in login.refusal(login.PERSONAL, "111", workers, None)
    assert login.already_known(ns(id=111, phone="111"), workers, None)
    assert login.refusal("worker", "333", workers, None) is None


def test_a_tdata_of_another_project_s_worker_is_refused(monkeypatch):
    green, blue = _project("Green"), _project("Blue")
    _write_jsession(Path(projects.sessions_dir(green)), "111", "w1")
    monkeypatch.setattr(tdata_import, "TDesktop", lambda path: ns(mainAccount=ns(UserId=111)))
    with pytest.raises(ValueError, match="уже воркер в проекте «Green»"):
        asyncio.run(tdata_import.convert("tdata", None, None, projects.sessions_dir(blue)))
    # in its own project: found offline, left as it is
    assert asyncio.run(tdata_import.convert("tdata", None, None, projects.sessions_dir(green))) == "111"


def test_the_mailing_stats_and_the_ledger_are_the_project_s_own():
    from functions.pmmailing import PmMailingFunc
    from modules import contacts_ledger

    green, blue = _project("Green"), _project("Blue")
    mailing = PmMailingFunc(projects.storage(green, 1, "h"), ns())
    mailing.load_stats()
    mailing.stats = {"5": {"count": 1}}
    mailing.save_stats()
    contacts_ledger.save(projects.home(green), {"5": 77})

    other = PmMailingFunc(projects.storage(blue, 1, "h"), ns())
    other.load_stats()
    assert other.stats == {} and contacts_ledger.load(projects.home(blue)) == {}
    assert other.filter_unsent([{"user_id": 5, "username": None}]) == [{"user_id": 5, "username": None}]


def test_cli_takes_a_free_account_into_the_project(monkeypatch):
    from functions import sign_in as cli

    green = _project("Green")
    os.makedirs(projects.FREE_DIR)
    _write_jsession(Path(projects.FREE_DIR), "222", "free1")
    _write_jsession(Path(projects.FREE_DIR), "223", "free2")
    _write_jsession(Path(projects.FREE_DIR), "224", "free3")
    said, answers = [], iter(["4", "1,3"])  # ➕ Добавить → [4] из свободных → the first and the third
    monkeypatch.setattr(cli, "say", said.append)
    monkeypatch.setattr(cli.console, "input", lambda *a: next(answers))
    storage = projects.storage(green, 1, "h")
    asyncio.run(cli.AddByPhoneFunc(storage, ns()).execute())
    assert said[-1] == "✅ Добавлено в проект: 2. Воркеров: 2."
    assert list(storage.full_sessions) == [os.path.join(projects.sessions_dir(green), f"{phone}.jsession")
                                           for phone in ("222", "224")]
    assert projects.account_files(projects.FREE_DIR) == [os.path.join(projects.FREE_DIR, "223.jsession")]


def test_cli_moves_a_worker_to_another_project(monkeypatch):
    from functions import accounts as cli

    green, blue = _project("Green"), _project("Blue")
    for phone in ("111", "112", "113"):
        _write_jsession(Path(projects.sessions_dir(green)), phone, f"w{phone}")
    storage = projects.storage(green, 1, "h")
    printed, answers = [], iter(["1-2", "1"])  # the first two → [1] 📁 Blue
    monkeypatch.setattr(cli.console, "print", lambda text="", *a, **k: printed.append(str(text)))
    monkeypatch.setattr(cli.console, "input", lambda *a: next(answers))
    asyncio.run(cli.MoveWorkersFunc(storage, ns()).execute())
    assert printed[3:5] == ["[1] 📁 Blue", "[2] 📦 В свободные"] and printed[-1].startswith("✅ Перенесено: 2 из 2.")
    assert len(storage) == 1 and projects.account_files(projects.sessions_dir(blue)) == [
        os.path.join(projects.sessions_dir(blue), f"{phone}.jsession") for phone in ("111", "112")]


def test_cli_card_sections_run_on_the_project_s_workers(monkeypatch):
    from functions import projects as cli
    from modules.storages.functions_storage import FunctionsStorage
    from test_scraper_menu import FakeSettings, FakeStorage, _answers

    _write_jsession(Path(projects.sessions_dir(_project("Green"))), "111", "w1")
    ran, printed = [], []
    monkeypatch.setattr(FunctionsStorage, "run_instance", lambda self, instance: ran.append(
        (type(instance).__name__, self.storage.home, len(self.storage))))
    monkeypatch.setattr(cli.TelethonFunction, "ask_int", staticmethod(lambda *a, **k: 1))  # [1] Green
    monkeypatch.setattr(cli.console, "print", lambda text="", *a, **k: printed.append(str(text)))
    _answers(monkeypatch, cli, texts=["9", "1"])  # 🤖 Аккаунты › → [1] 📋 Список
    cli.ProjectsFunc(FakeStorage(), FakeSettings()).execute()
    assert any("[9] 🤖 Аккаунты ›  [10] 💬 Активность ›" in p for p in printed)  # in the bot's order
    assert ran == [("AccountsFunc", projects.home(projects.find("Green")), 1)]
