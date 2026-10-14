"""Projects (modules/projects.py): the file, its backups and names, a project's files, and the
bot's and the CLI's projects screens on the same model, a project's scrape included."""

import asyncio
import inspect
import json
import os
from types import SimpleNamespace as ns

import pandas as pd
import pytest

from bot.callbacks import ChoiceCB
from bot.services.jobs import JobManager
from bot.services.registry import RISK_NOTE
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
        assert json.load(file) == [{"name": "Neo", "channels": ["@x", "@y"], "chats": {}}]


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


def test_a_project_may_have_no_channels():
    """Its base comes from groups' members or a list; the scrape waits for channels."""
    assert projects.add("A", []).channels == []
    assert projects.set_channels("A", ["@a"]).channels == ["@a"]
    assert projects.set_channels("A", []).channels == [] and projects.load()[0].channels == []
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
    assert "📂 Файлов скрапа пока нет." in projects.card(project)
    assert projects.label(project) == "📁 Green · каналов: 2"

    _write_parquet(tmp_path / "Green_posts_01.01.2024-31.01.2024.parquet", rows=3)
    _checkpoint(tmp_path, "Green", t_index=1240)
    card = projects.card(project)
    assert card.startswith("📁 Green\n\n📢 Каналы — 2\n\n1. @a\n❔ название — при «▶️ Скрап проекта»\n\n2. @b\n")
    assert "собрано постов 1240" in card
    assert f"📂 Файлы в {tmp_path} — 1\n• posts · 01.01.2024-31.01.2024 · 3" in card
    assert projects.label(project) == "📁 Green · каналов: 2 ⏸"


def test_card_lists_at_most_max_card_files(tmp_path):
    project = projects.add("Green", ["@a"])
    for day in range(1, projects.MAX_CARD_FILES + 6):
        _write_parquet(tmp_path / f"Green_posts_{day:02}.01.2024-31.01.2024.parquet")
    card = projects.card(project)
    assert card.count("\n• posts · ") == projects.MAX_CARD_FILES and card.endswith("… и ещё 5")


def test_bot_card_fits_a_message_with_long_channel_titles(tmp_path):
    from scraper.scrape import ChatInfo

    channels = [f"@chan{i:02}" for i in range(projects.MAX_CARD_CHANNELS)]
    projects.add("Green", channels)
    projects.remember("Green", [ChatInfo(raw, "Т" * 128 + " › " + "Т" * 100, None, -1001234567890 - i, raw[1:])
                                for i, raw in enumerate(channels)])
    for day in range(1, projects.MAX_CARD_FILES + 1):
        _write_parquet(tmp_path / f"Green_posts_{day:02}.01.2024-31.01.2024.parquet")
    [project] = projects.load()
    card = projects.card(project, html=True)
    assert len(card) <= projects.MAX_CARD_TEXT and "\n\n… и ещё " in card
    assert card.count("\n• posts · ") == projects.MAX_CARD_FILES  # the files stay whole
    assert f"\n\n{projects.MAX_CARD_CHANNELS}. " in projects.card(project)  # the terminal: every channel


def test_importable_offers_the_finished_scrapes_with_no_project_yet(tmp_path):
    _posts(tmp_path, "Old", 1_000, channels=["@o"])
    _posts(tmp_path, "Green", 1_500, channels=["@a"]).rename(tmp_path / "Green_posts_2023.parquet")  # older, same name
    _posts(tmp_path, "Green", 2_000, channels=["@a", "t.me/+inv"])
    _posts(tmp_path, "Mute", 3_000, groups=())       # no channels in the file: a project without them
    _posts(tmp_path, "Taken", 4_000, channels=["@t"])
    club = tmp_path / "Club_participants_members.parquet"  # a members base alone: a project without channels
    _write_parquet(club)
    os.utime(club, (5_000, 5_000))
    projects.add("taken", ["@t"])
    assert projects.importable() == [("Club", []), ("Mute", []), ("Green", ["@a", "t.me/+inv"]),
                                     ("Old", ["@o"])]  # newest first


# --- the bot ----------------------------------------------------------------------------------

def _router():
    from bot.routers import projects as router
    return router


def _act(action, state):
    """A tap on the shown card's `action` button: the card's buttons carry its token (a state made
    by hand, standing for a card shown, gets one)."""
    token = state.data.setdefault("card_token", "t0ken")
    return ChoiceCB(scope="prj_act", value=f"{action}.{token}")


def test_bot_with_no_projects_offers_a_new_one():
    router = _router()
    start = _SCallback()
    asyncio.run(router.projects_screen(start.message, _State(), False))
    text, markup = start.message.answers[0]
    assert text.startswith("📁 <b>Проекты</b> — пока ни одного") and "Без проекта" not in text
    assert _buttons(markup) == ["➕ Новый проект"]  # all the work is a project's


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
    # collect, check, work — every function, files or not (one with nothing to work on says what to collect
    # first) —, then ⚙️ to the project itself
    assert text.startswith("📁 Green") and _buttons(markup) == [
        "▶️ Скрап проекта", "👥 Участники чата", "📥 Загрузить список", "✅ Верификация", "📈 Анализ",
        "⚠️ 📇 В контакты", "⚠️ 📨 ЛС по базе", "⚙️ Управление проектом", "⬅️ Назад"]
    assert [len(row) for row in markup.inline_keyboard] == [1, 2, 2, 2, 1, 1]
    token = state.data.pop("card_token")
    assert state.data == {"project_name": "Green", "project_names": []}
    # every button of the card carries the card's token
    assert {ChoiceCB.unpack(button.callback_data).value.rpartition(".")[2]
            for row in markup.inline_keyboard for button in row} == {token}


def test_bot_lists_projects_and_opens_one_by_its_button():
    router = _router()
    projects.add("A", ["@a"])
    projects.add("B", ["@b", "@c"])
    state = _State()
    start = _SCallback()
    asyncio.run(router.projects_screen(start.message, state, False))
    assert _buttons(start.message.answers[0][1]) == ["📁 A · каналов: 1", "📁 B · каналов: 2", "➕ Новый проект"]

    pick = _SCallback()
    asyncio.run(router.project_pick(pick, ChoiceCB(scope="prj", value="1"), state))
    assert pick.message.answers == [] and pick.message.edits[0][0].startswith("📁 B")  # the list becomes the card

    back = _SCallback()
    asyncio.run(router.project_action(back, _act("back", state), state, _pool(), None, None))
    assert back.message.answers == [] and _buttons(back.message.edits[0][1])[:2] == ["📁 A · каналов: 1",
                                                                                      "📁 B · каналов: 2"]


def test_bot_list_buttons_still_work_after_a_card_is_opened():
    router = _router()
    projects.add("A", ["@a"])
    projects.add("B", ["@b"])
    state = _State()
    asyncio.run(router.projects_screen(_SMsg(), state, False))
    asyncio.run(router.project_pick(_SCallback(), ChoiceCB(scope="prj", value="0"), state))
    again = _SCallback()  # the list's message above, its other button
    asyncio.run(router.project_pick(again, ChoiceCB(scope="prj", value="1"), state))
    assert again.message.edits[0][0].startswith("📁 B")


def test_bot_makes_a_project_from_a_finished_scrape(tmp_path):
    router = _router()
    _posts(tmp_path, "Green", 1_000, channels=["@a", "@b"])
    state = _State()
    start = _SCallback()
    asyncio.run(router.projects_screen(start.message, state, False))
    assert _buttons(start.message.answers[0][1]) == ["➕ Новый проект", "📥 Из готового скрапа"]

    offer = _SCallback()
    asyncio.run(router.project_pick(offer, ChoiceCB(scope="prj", value="import"), state))
    assert offer.message.answers == []  # in the list's place
    assert _buttons(offer.message.edits[0][1]) == ["📁 Green · каналов: 2", "⬅️ Назад"]

    back = _SCallback()
    asyncio.run(router.project_import(back, ChoiceCB(scope="prj_imp", value="back"), state))
    assert back.message.edits[0][0].startswith("📁 <b>Проекты</b>") and projects.load() == []

    asyncio.run(router.project_pick(_SCallback(), ChoiceCB(scope="prj", value="import"), state))  # again
    pick = _SCallback()
    asyncio.run(router.project_import(pick, ChoiceCB(scope="prj_imp", value="0"), state))
    assert projects.load() == [projects.Project("Green", ["@a", "@b"])]
    card = pick.message.edits[0][0]
    assert card.startswith("📁 Green") and "• posts · 01.01.2024-31.01.2024 · 1" in card

    nothing = _SCallback()  # Green has its project now
    asyncio.run(router.project_pick(nothing, ChoiceCB(scope="prj", value="import"), state))
    assert nothing.message.answers[0][0].startswith("Не из чего сделать проект")


def test_bot_makes_a_project_without_channels_by_its_button():
    from bot.routers._common import STALE_BUTTON

    router = _router()
    state = _State()
    asyncio.run(router.project_pick(_SCallback(), ChoiceCB(scope="prj", value="new"), state))
    asked = _Msg(text="Clients")
    asyncio.run(router.project_name(asked, state))
    text, markup = asked.answers[0]
    assert "Нет каналов? Нажмите «📭 Без каналов»" in text and _buttons(markup) == ["📭 Без каналов", "✖️ Отмена"]
    none = ChoiceCB.unpack(markup.inline_keyboard[0][0].callback_data)

    tap = _SCallback()
    asyncio.run(router.project_without_channels(tap, none, state))
    assert projects.load() == [projects.Project("Clients", [])]
    text, markup = tap.message.answers[0]
    # no ▶️ Скрап without channels; ⚙️'s button says what it does now
    assert "📢 Каналов нет" in text and "«➕ Добавить каналы»" in text and _buttons(markup) == [
        "▶️ Скрап проекта", "👥 Участники чата", "📥 Загрузить список", "✅ Верификация", "📈 Анализ",
        "⚠️ 📇 В контакты", "⚠️ 📨 ЛС по базе", "⚙️ Управление проектом", "⬅️ Назад"]
    manage = _SCallback()
    asyncio.run(router.project_action(manage, _act("manage", state), state, _pool(), None, None))
    assert _buttons(manage.message.edits[0][1]) == ["➕ Добавить каналы", "🏷 Переименовать", "🗑 Удалить", "⬅️ Назад"]

    listed = _SCallback()
    asyncio.run(router.projects_screen(listed.message, state, False))
    assert _buttons(listed.message.answers[0][1])[0] == "📁 Clients · без каналов"

    again = _SCallback()  # the prompt's button once the chat has left its step: nothing is made
    asyncio.run(router.project_without_channels(again, none, state))
    assert again.message.answers == [(STALE_BUTTON, None)] and len(projects.load()) == 1


def test_bot_drops_all_the_channels_by_its_button():
    router = _router()
    projects.add("A", ["@a"])
    state = _State({"project_name": "A"})
    ask = _SCallback()
    asyncio.run(router.project_action(ask, _act("channels", state), state, _pool(), None, None))
    markup = ask.message.answers[0][1]
    assert _buttons(markup) == ["📭 Убрать все каналы", "✖️ Отмена"]

    tap = _SCallback()
    asyncio.run(router.project_without_channels(tap, ChoiceCB.unpack(markup.inline_keyboard[0][0].callback_data),
                                                state))
    assert projects.load()[0].channels == [] and "📢 Каналов нет" in tap.message.answers[0][0]

    ask = _SCallback()  # none left: nothing to drop, only to add
    asyncio.run(router.project_action(ask, _act("channels", state), state, _pool(), None, None))
    text, markup = ask.message.answers[0]
    assert text.startswith("Сейчас каналов нет") and _buttons(markup) == ["✖️ Отмена"]


def test_a_project_s_label_says_it_has_no_channels():
    assert projects.label(projects.Project("A", ["@a", "@b"])) == "📁 A · каналов: 2"
    assert projects.label(projects.Project("B", [])) == "📁 B · без каналов"


def test_cli_card_of_a_project_without_channels_offers_to_add_them(monkeypatch):
    projects.add("Green", [])
    _, printed = _cli_card(monkeypatch, texts=["1"])  # ▶️ Скрап: no channels to scrape
    assert "📁 Green · без каналов" in "\n".join(printed)
    menu = next(line for line in printed if "[3] 👥" in line)
    assert "[9] ➕ Добавить каналы" in menu
    assert printed[-1] == "У проекта нет каналов — добавьте их: «➕ Добавить каналы»."


def test_bot_edits_channels_and_deletes_a_project_keeping_its_files(tmp_path, monkeypatch):
    router = _router()
    monkeypatch.setattr(scraped_files, "BASES_DIR", str(tmp_path))
    posts = tmp_path / "A_posts_01.01.2024-31.01.2024.parquet"
    _write_parquet(posts)
    projects.add("A", ["@a"])
    state = _State({"project_name": "A"})

    asyncio.run(router.project_action(_SCallback(), _act("channels", state), state,
                                      _pool(), None, None))
    edited = _Msg(text="@x @y")
    asyncio.run(router.project_edit(edited, state))
    assert projects.load()[0].channels == ["@x", "@y"] and "📢 Каналы — 2\n\n1. @x\n" in edited.answers[0][0]

    asyncio.run(router.project_action(_SCallback(), _act("delete", state), state,
                                      _pool(), None, None))
    keep = _SCallback()
    asyncio.run(router.project_delete(keep, ChoiceCB(scope="prj_del", value="no"), state))
    assert projects.load() and keep.message.edits[0][0].startswith("⚙️ <b>Управление проектом «A»</b>")  # back

    confirm = _SCallback()
    asyncio.run(router.project_action(confirm, _act("delete", state), state,
                                      _pool(), None, None))
    gone = _SCallback()
    asyncio.run(router.project_delete(gone, ChoiceCB(scope="prj_del", value=_delete_value(confirm)), state))
    assert projects.load() == [] and posts.exists()
    assert "удалён, файлы скрапа на месте" in gone.message.edits[0][0]


def test_bot_stale_card_button_is_reported():
    router = _router()
    stale = _SCallback()
    asyncio.run(router.project_action(stale, ChoiceCB(scope="prj_act", value="scrape"), _State(),
                                      _pool(), None, None))
    assert stale.message.answers[0][0].startswith("Кнопка устарела — откройте 📁 Проекты заново")


def test_bot_an_older_card_s_button_does_not_act_on_the_project_opened_since():
    router = _router()
    projects.add("A", ["@a"])
    projects.add("B", ["@b"])
    state = _State()
    asyncio.run(router.projects_screen(_SMsg(), state, False))
    card_a = _SCallback()
    asyncio.run(router.project_pick(card_a, ChoiceCB(scope="prj", value="0"), state))  # A's card
    asyncio.run(router.project_action(card_a, _act("manage", state), state, _pool(), None, None))  # its ⚙️
    rename_a = next(ChoiceCB.unpack(button.callback_data) for row in card_a.message.edits[1][1].inline_keyboard
                    for button in row if button.text == "🏷 Переименовать")
    asyncio.run(router.projects_screen(_SMsg(), state, False))
    asyncio.run(router.project_pick(_SCallback(), ChoiceCB(scope="prj", value="1"), state))  # B's card

    old = _SCallback()  # 🏷 on A's card above
    asyncio.run(router.project_action(old, rename_a, state, _pool(), None, None))
    assert old.message.answers[0][0].startswith("Кнопка устарела — откройте 📁 Проекты заново")
    assert state.state is None  # nothing asked: not B's new name


def test_bot_card_s_manage_screen_takes_its_place_and_goes_back_to_it():
    router = _router()
    projects.add("A", ["@a"])
    state = _State()
    asyncio.run(router.projects_screen(_SMsg(), state, False))
    card = _SCallback()
    asyncio.run(router.project_pick(card, ChoiceCB(scope="prj", value="0"), state))
    token = state.data["card_token"]
    asyncio.run(router.project_action(card, _act("manage", state), state, _pool(), None, None))
    text, markup = card.message.edits[1]  # in the card's place
    # each button with what it does, as on the other screens; 🧹 neither shown nor described: nothing to clear
    assert text == ("⚙️ <b>Управление проектом «A»</b>\n\n"
                    "<b>✏️ Каналы</b> — список каналов и групп проекта для скрапа\n"
                    "<b>🏷 Переименовать</b> — новое имя проекта, его файлы переименуются вместе с ним\n"
                    "<b>🗑 Удалить</b> — проект удаляется, файлы скрапа остаются") and card.message.answers == []
    assert _buttons(markup) == ["✏️ Каналы", "🏷 Переименовать", "🗑 Удалить", "⬅️ Назад"]
    assert [len(row) for row in markup.inline_keyboard] == [2, 1, 1]
    assert {ChoiceCB.unpack(button.callback_data).value.rpartition(".")[2]
            for row in markup.inline_keyboard for button in row} == {token}  # the card's own

    back = next(ChoiceCB.unpack(b.callback_data) for row in markup.inline_keyboard for b in row if b.text == "⬅️ Назад")
    asyncio.run(router.project_action(card, back, state, _pool(), None, None))
    assert card.message.edits[2][0].startswith("📁 A") and "⚙️ Управление проектом" in _buttons(card.message.edits[2][1])


def _delete_value(confirm):
    """The «🗑 Удалить» button's value on the confirmation that took the card's place."""
    return ChoiceCB.unpack(confirm.message.edits[-1][1].inline_keyboard[0][0].callback_data).value


def test_bot_an_old_delete_confirmation_does_not_delete_the_project_opened_since():
    router = _router()
    projects.add("A", ["@a"])
    projects.add("B", ["@b"])
    state = _State()
    asyncio.run(router.projects_screen(_SMsg(), state, False))
    asyncio.run(router.project_pick(_SCallback(), ChoiceCB(scope="prj", value="0"), state))  # A's card
    confirm_a = _SCallback()
    asyncio.run(router.project_action(confirm_a, _act("delete", state), state,
                                      _pool(), None, None))  # «Удалить проект «A»?»
    asyncio.run(router.project_pick(_SCallback(), ChoiceCB(scope="prj", value="1"), state))  # B's card

    old = _SCallback()  # 🗑 on A's confirmation above
    asyncio.run(router.project_delete(old, ChoiceCB(scope="prj_del", value=_delete_value(confirm_a)), state))
    assert [p.name for p in projects.load()] == ["A", "B"]
    assert old.message.answers[0][0].startswith("Кнопка устарела — откройте 📁 Проекты заново")

    # B's own confirmation shown meanwhile: still not A's button that deletes B
    asyncio.run(router.project_action(_SCallback(), _act("delete", state), state,
                                      _pool(), None, None))  # «Удалить проект «B»?»
    old = _SCallback()
    asyncio.run(router.project_delete(old, ChoiceCB(scope="prj_del", value=_delete_value(confirm_a)), state))
    assert [p.name for p in projects.load()] == ["A", "B"]
    assert old.message.answers[0][0].startswith("Кнопка устарела — откройте 📁 Проекты заново")


def test_bot_reports_an_unreadable_file():
    router = _router()
    with open(projects.PATH, "w", encoding="utf-8") as file:
        file.write("{broken")
    start = _SCallback()
    asyncio.run(router.projects_screen(start.message, _State(), False))
    assert "не читается" in start.message.answers[0][0]


def test_bot_project_scrape_skips_name_and_channels_and_runs_on_them(monkeypatch, tmp_path):
    from bot.routers import scraping
    from bot.states import Scrape

    router = _router()
    monkeypatch.setattr(scraping, "DEFAULT_OUT", str(tmp_path))
    monkeypatch.setattr(scraped_files, "BASES_DIR", str(tmp_path))
    calls = _scrapes(monkeypatch)
    projects.add("Green", ["@a", "t.me/b"])
    state = _State({"project_name": "Green"})

    go = _SCallback()
    asyncio.run(router.project_action(go, _act("scrape", state), state, _pool(), None,
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
    from bot.states import Scrape

    router = _router()
    monkeypatch.setattr(scraping, "DEFAULT_OUT", str(tmp_path))
    monkeypatch.setattr(scraped_files, "BASES_DIR", str(tmp_path))
    _checkpoint(tmp_path, "Green")
    projects.add("Green", ["@new"])
    state = _State({"project_name": "Green"})

    go = _SCallback()
    asyncio.run(router.project_action(go, _act("scrape", state), state, _pool(), None,
                                      JobManager()))
    assert state.state == Scrape.resume and "незавершённый скрап «Green»" in go.message.answers[0][0]

    fresh = _SCallback()
    asyncio.run(scraping.scrape_resume(fresh, ChoiceCB(scope="scrape_resume", value="fresh"), state, _pool(),
                                       None, JobManager()))
    assert state.state == Scrape.date_min and state.data["channels"] == "@new"  # not asked again


# --- the CLI ----------------------------------------------------------------------------------

def test_cli_menu_discovers_only_the_projects_function():
    from functions import projects as cli
    from bot.services.registry import CLI_EXTRAS

    # the discovery takes every *Func class in the module: ScrapeFunc must not be one twice
    assert [name for name, _ in inspect.getmembers(cli, inspect.isclass) if name.endswith("Func")] == ["ProjectsFunc"]
    assert [f.title for f in CLI_EXTRAS if f.classname == "ProjectsFunc"] == ["📁 Проекты"]  # a CLI item only


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
    _answers(monkeypatch, cli, texts=["9", "@x"])
    cli.ProjectsFunc(FakeStorage(), FakeSettings()).execute()
    assert projects.load()[0].channels == ["@x"]

    _answers(monkeypatch, cli, texts=["12"], bools=[True])
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


def test_cli_card_shows_every_function_and_says_what_to_collect_first(monkeypatch, tmp_path):
    projects.add("Green", ["@a"])
    _, printed = _cli_card(monkeypatch, ["5"])  # no files yet: every item still there
    assert any("[5] ✅ Верификация  [6] 📈 Анализ  [7] ⚠️ 📇 В контакты  [8] ⚠️ 📨 ЛС по базе" in p for p in printed)
    assert printed[-1] == "У проекта ещё нет файла постов — сначала «▶️ Скрап проекта»."
    _, printed = _cli_card(monkeypatch, ["7"])
    assert printed[-1].startswith("У проекта ещё нет базы участников — сначала «▶️ Скрап проекта»")


def test_cli_card_numbers_stay_put_with_nothing_to_top_up_or_clear(monkeypatch, tmp_path):
    from functions import scraper

    projects.add("Green", ["@a"])
    called = []
    monkeypatch.setattr(scraper.ScrapeFunc, "topup", lambda self, *a: called.append("topup"))
    _, printed = _cli_card(monkeypatch, ["2"])  # 🔄 hidden: its number does nothing
    menu = next(p for p in printed if "[1] ▶️ Скрап проекта" in p)
    assert "[2]" not in menu and "[11]" not in menu and called == []
    assert "[3] 👥 Участники чата  [4] 📥 Загрузить список" in menu
    assert "⚙️ Управление проектом:  [9] ✏️ Каналы  [10] 🏷 Переименовать  [12] 🗑 Удалить" in menu


def test_cli_card_collects_members_as_the_project_s_base(monkeypatch, tmp_path):
    from functions import scraper

    projects.add("Green", ["@a"])
    called = []
    monkeypatch.setattr(scraper.MembersFunc, "execute", lambda self, project=None: called.append(project))
    _, printed = _cli_card(monkeypatch, ["3"])
    assert any("[3] 👥 Участники чата" in p for p in printed) and called == ["Green"]


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

    for module, cls, action in ((pmmailing, "PmMailingFunc", "8"), (add_contacts, "AddContactsFunc", "7")):
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
        _cli_card(monkeypatch, ["5"])
    assert offered == [[str(green)]]

    # 📈 Анализ → 🧩 Объединить ([4]), blank: all the project's posts files, as the bot's button
    combined = []
    monkeypatch.setattr(scraper_analysis.analysis, "combine", lambda inp, out, dedup: combined.append(inp))
    _cli_card(monkeypatch, ["6", "4", "", "out", "Group,Message ID"])
    assert combined == [[str(green)]]


def test_cli_combine_takes_the_project_s_posts_files_by_default(monkeypatch, tmp_path):
    from functions import scraper_analysis

    green = _posts(tmp_path, "Green", 1_000)
    _write_parquet(tmp_path / "Green_participants_01.01.2024-31.01.2024.parquet")
    _write_parquet(tmp_path / "Green_reactors_01.01.2024-31.01.2024.parquet")
    combined = []
    monkeypatch.setattr(scraper_analysis.analysis, "combine", lambda inp, out, dedup: combined.append(inp))
    monkeypatch.setattr(scraper_analysis.Prompt, "ask", lambda *a, default=None, **k: default)  # Enter each time

    _posts(tmp_path, "Other", 2_000)  # another project's
    scraper_analysis._combine("Green")
    assert combined == [[str(green)]]  # the project's posts files: its bases are not even read


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
    # one block each, as the card lists its own channels
    assert "Каналы дособора — как в прошлом скрапе (новому каналу нужна вся история: «▶️ Скрап проекта»):\n\n1. @old" \
        in card

    _checkpoint(tmp_path, "Green")  # unfinished: continuing it comes first
    assert "🔄" not in projects.card(project)


def test_bot_top_up_goes_from_the_card_to_the_summary_and_runs_on_the_files_data(monkeypatch, tmp_path):
    from bot.routers import scraping
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
    asyncio.run(router.project_action(go, _act("topup", state), state, _pool(), None,
                                      JobManager()))
    text, markup = go.message.answers[-1]
    assert state.state == Scrape.confirm and text.startswith("Каналы:\n\n1. @a\n\nПериод: 24.01.2024")
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
    _answers(monkeypatch, cli, texts=["2"], bools=[True, False, True])  # 🔄; comments, no reactors, base
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
    callback = _SCallback()
    asyncio.run(_router().project_action(callback, _act(action, state), state,
                                         pool or _pool(), None, JobManager()))
    return callback


class _AlertCallback(_SCallback):
    """A tap that keeps what the bot answered it with: a pop-up's text."""

    def __init__(self):
        super().__init__()
        self.alerts = []

    async def answer(self, text=None, show_alert=False, **kwargs):
        self.alerts.append((text, show_alert))


def test_card_shows_every_function_and_says_what_to_collect_first(tmp_path):
    router = _router()
    project = projects.add("Green", ["@a"])
    card = _SCallback()
    asyncio.run(router._show_card(card.message, _State(), project))
    text, markup = card.message.answers[0]
    # no files yet: every function, the risky ones marked, and the mark explained
    assert _buttons(markup) == [
        "▶️ Скрап проекта", "👥 Участники чата", "📥 Загрузить список", "✅ Верификация", "📈 Анализ",
        "⚠️ 📇 В контакты", "⚠️ 📨 ЛС по базе", "⚙️ Управление проектом", "⬅️ Назад"]
    assert text.endswith(f"<i>{RISK_NOTE}</i>")

    state = _State({"project_name": "Green"})
    for action, why in (("verify", "У проекта ещё нет файла постов — сначала «▶️ Скрап проекта»."),
                        ("mail", "У проекта ещё нет базы — сначала «▶️ Скрап проекта», «👥 Участники чата» или "
                                 "«📥 Загрузить список».")):
        tap = _AlertCallback()
        asyncio.run(router.project_action(tap, _act(action, state), state, _pool(), None, None))
        assert tap.alerts == [(why, True)] and tap.message.answers == []  # a pop-up over the card, nothing run
        assert state.state is None

    _scraped_files(tmp_path)  # a base and a posts file: they run
    tap = _AlertCallback()
    asyncio.run(router.project_action(tap, _act("verify", state), state, _pool(), None, None))
    assert tap.alerts == [(None, False)] and tap.message.answers


def test_card_mailing_offers_the_project_s_bases_only(tmp_path):
    from bot.routers import broadcasts
    from bot.states import PmMailing

    _two_projects(tmp_path)
    state = _State({"project_name": "Green"})
    callback = _card_action("mail", state)
    text, markup = callback.message.answers[0]
    assert _buttons(markup) == ["📄 participants · 01.01.2024-31.01.2024 · 2", "✖️ Отмена"]
    assert state.state == PmMailing.path

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
    assert _buttons(callback.message.answers[0][1]) == ["📄 participants · 01.01.2024-31.01.2024 · 2", "✖️ Отмена"]
    asyncio.run(audience.run_base(_SCallback(), ChoiceCB(scope="contacts_base", value="0"), state, _pool(),
                                  {}, None, None))
    assert launched == [str(tmp_path / "Green_participants_01.01.2024-31.01.2024.parquet")]


def test_card_verify_offers_the_project_s_posts_only(tmp_path):
    _two_projects(tmp_path)
    callback = _card_action("verify", _State({"project_name": "Green"}))  # one account: taken at once
    assert _buttons(callback.message.answers[-1][1]) == ["📄 posts · 01.01.2024-31.01.2024 · 2", "✖️ Отмена"]


def test_card_analysis_offers_the_project_s_files_only(tmp_path):
    from bot.routers import scraping

    _two_projects(tmp_path)
    state = _State({"project_name": "Green"})
    menu = _card_action("analysis", state).message.answers[-1][1]
    links = _SCallback()
    asyncio.run(scraping.analysis_tool(links, _tap(menu, "🔗 Ссылки на другие каналы"), state))
    assert _buttons(links.message.answers[0][1]) == ["📄 posts · 01.01.2024-31.01.2024 · 2", "✖️ Отмена"]
    read = _SCallback()
    asyncio.run(scraping.analysis_tool(read, _tap(menu, "👀 Посмотреть файл"), state))
    assert sorted(_buttons(read.message.answers[0][1])[:-1]) == ["📄 participants · 01.01.2024-31.01.2024 · 2",
                                                                 "📄 posts · 01.01.2024-31.01.2024 · 2"]


def _tap(markup, label):
    """The callback data of a keyboard's button."""
    return ChoiceCB.unpack(next(b for row in markup.inline_keyboard for b in row if b.text == label).callback_data)


def test_project_analysis_menu_keeps_its_project(tmp_path):
    """A project's analysis menu works on that project's files after a run too; once another
    project's menu is opened, its buttons are stale — never the other project's files."""
    from bot.routers import scraping
    from bot.routers._common import STALE_BUTTON

    _two_projects(tmp_path)
    other = projects.add("Other", ["@o"])
    state = _State({"project_name": "Green"})
    menu = _card_action("analysis", state).message.answers[-1][1]

    read = _SCallback()  # a run: 👀 the project's posts file
    asyncio.run(scraping.analysis_tool(read, _tap(menu, "👀 Посмотреть файл"), state))
    asyncio.run(scraping.analysis_file_pick(read, ChoiceCB(scope="an_file", value="0"), state, JobManager()))
    assert read.message.answers[-1][0] == "Готово ✅"

    links = _SCallback()  # the same menu after the run: still the project's files only
    asyncio.run(scraping.analysis_tool(links, _tap(menu, "🔗 Ссылки на другие каналы"), state))
    assert _buttons(links.message.answers[0][1]) == ["📄 posts · 01.01.2024-31.01.2024 · 2", "✖️ Отмена"]

    asyncio.run(scraping.analysis_project(_SCallback().message, state, other))  # Other's menu since
    stale = _SCallback()
    asyncio.run(scraping.analysis_tool(stale, _tap(menu, "🔗 Ссылки на другие каналы"), state))
    assert [text for text, _ in stale.message.answers] == [STALE_BUTTON]


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
    asyncio.run(router.project_rename(bad, state, JobManager(), JobManager()))
    assert "Не подходит: проект с таким именем уже есть" in bad.answers[0][0] and state.state == Projects.rename

    done = _Msg(text="Blue")
    asyncio.run(router.project_rename(done, state, JobManager(), JobManager()))
    assert done.answers[0][0] == f"✅ Проект «Green» теперь «Blue». Переименовано файлов и папок: {len(GREEN) + 2}."
    assert done.answers[1][0].startswith("📁 Blue")
    assert state.data["project_names"] == ["Blue", "Green_x"]  # the list's buttons follow the rename


@pytest.mark.parametrize("slot", ["scrapes", "analyses"])
def test_bot_wont_rename_while_a_scraper_or_analysis_job_runs(tmp_path, slot):
    """A members list, a verify or an analysis writes its file under the name it started with: a
    rename meanwhile would leave that file outside the project (they set no scrape marker)."""
    from bot.states import Projects

    router = _router()
    _green_on_disk(tmp_path)
    before = _bases(tmp_path)
    jobs = {"scrapes": JobManager(), "analyses": JobManager()}
    assert jobs[slot].acquire("Участники чата", cancelable=False, timeout=0)
    state = _State({"project_name": "Green", "project_names": ["Green", "Green_x"]})
    state.state = Projects.rename

    msg = _Msg(text="Blue")
    asyncio.run(router.project_rename(msg, state, jobs["scrapes"], jobs["analyses"]))

    assert msg.answers[0][0] == "⛔ Идёт «Участники чата» — переименуйте проект после её окончания."
    assert state.state == Projects.rename  # the name can be sent again
    assert _bases(tmp_path) == before and [p.name for p in projects.load()] == ["Green", "Green_x"]


def test_cli_renames_a_project(monkeypatch, tmp_path):
    from functions import projects as cli

    _green_on_disk(tmp_path)
    monkeypatch.setattr(cli.TelethonFunction, "ask_int", staticmethod(lambda *a, **k: 1))  # [1] Green
    _answers(monkeypatch, cli, texts=["10", "Blue"])
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


# --- the channels' names on the card ----------------------------------------------------------

def _info(raw, title=None, problem=None, chat_id=None, username=None):
    from scraper.scrape import ChatInfo

    return ChatInfo(raw, title, problem, chat_id, username)


def test_names_from_a_check_keep_the_operator_s_last_change_undoable():
    """projects.json.bak is the undo of the operator's last change (README): the names a check writes
    on its own after it must not replace it."""
    projects.add("Green", ["@a", "@b"])
    projects.set_channels("Green", ["@a"])  # a change the operator may want to undo
    projects.remember("Green", [_info("@a", "«Канал А»", None, -1001, "a")])

    assert projects.load()[0].chats == {"@a": {"title": "«Канал А»", "id": -1001, "username": "a"}}
    with open(projects.PATH + ".bak", encoding="utf-8") as file:
        assert [p["channels"] for p in json.load(file)] == [["@a", "@b"]]


def test_a_check_names_every_chat_of_the_project_s_work_and_writes_only_a_change():
    projects.add("Green", ["@a", "-1001629147115", "t.me/+join", "@x"])
    projects.remember("Green", [
        _info("@a", "«Канал А»", None, -1001, "a"),
        _info("-1001629147115", "«Цитадель»", None, -1001629147115),
        _info("t.me/+join", "«Клуб»", "аккаунт не состоит в чате — вступите"),  # an invite: its title
        _info("@other", "«Чужой»", None, -1009, "other"),  # a list corrected for one run, a group of 👥
        _info("@x", None, "нет такого username"),  # not named: nothing kept
    ])
    [project] = projects.load()
    assert project.chats == {
        "@a": {"title": "«Канал А»", "id": -1001, "username": "a"},
        "-1001629147115": {"title": "«Цитадель»", "id": -1001629147115, "username": None},
        "t.me/+join": {"title": "«Клуб»", "id": None, "username": None},
        "@other": {"title": "«Чужой»", "id": -1009, "username": "other"},  # shown by name in the flows too
    }
    assert projects.chat_list(project, ["@other", "@x"]) == "1. «Чужой»\n👤 @other · 🆔 -1009\n\n2. @x"
    mtime = os.stat(projects.PATH).st_mtime_ns
    projects.remember("Green", [_info("@a", "«Канал А»", None, -1001, "a")])  # nothing new
    projects.remember("Nope", [_info("@a", "«Канал А»")])  # no such project
    assert os.stat(projects.PATH).st_mtime_ns == mtime


def test_new_channels_keep_the_names_checked_and_an_old_file_still_reads():
    with open(projects.PATH, "w", encoding="utf-8") as file:  # written before the names were kept
        json.dump([{"name": "Green", "channels": ["@a", "@b"]}], file)
    assert projects.load() == [projects.Project("Green", ["@a", "@b"])]
    projects.remember("Green", [_info("@a", "«Канал А»", None, -1001, "a"), _info("@b", "«Канал Б»")])
    # a channel dropped and added back, or shown in a flow, is still named
    assert set(projects.set_channels("Green", ["@b", "@c"]).chats) == {"@a", "@b"}


def test_the_card_lists_the_channels_as_the_accounts_list_does(tmp_path, monkeypatch):
    monkeypatch.setattr(scraped_files, "BASES_DIR", str(tmp_path))
    projects.add("Green", ["@a", "-1001629147115", "t.me/+join", "@new"])
    projects.remember("Green", [_info("@a", "«Канал А»", None, -1001, "a"),
                                _info("-1001629147115", "«Цитадель»", None, -1001629147115),
                                _info("t.me/+join", "«Клуб»", "аккаунт не состоит в чате — вступите")])
    [project] = projects.load()
    assert projects.card(project) == (
        "📁 Green\n\n📢 Каналы — 4\n\n"
        "1. «Канал А»\n👤 @a · 🆔 -1001\n\n"
        "2. «Цитадель»\n🆔 -1001629147115\n\n"
        "3. «Клуб»\n🔗 t.me/+join\n\n"  # no problem kept: it is the checking account's
        "4. @new\n❔ название — при «▶️ Скрап проекта»\n\n"
        "📂 Файлов скрапа пока нет.")


def test_a_card_lists_twenty_channels_then_counts_the_rest(tmp_path, monkeypatch):
    monkeypatch.setattr(scraped_files, "BASES_DIR", str(tmp_path))
    project = projects.add("Big", [f"@c{i}" for i in range(25)])
    card = projects.card(project)
    assert "20. @c19\n" in card and "@c20" not in card and "… и ещё 5" in card


def test_the_bot_s_project_scrape_names_the_channels_on_its_card(monkeypatch, tmp_path):
    from bot.routers import scraping

    router = _router()
    monkeypatch.setattr(scraping, "DEFAULT_OUT", str(tmp_path))
    monkeypatch.setattr(scraped_files, "BASES_DIR", str(tmp_path))

    async def lookup(message, pool, personal, path, chats):
        return [_info("@a", "«Канал А»", None, -1001, "a"), _info("t.me/b", "«Канал Б»", None, -1002)]

    monkeypatch.setattr(scraping, "_lookup", lookup)
    projects.add("Green", ["@a", "t.me/b"])
    state = _State({"project_name": "Green"})
    go = _SCallback()
    asyncio.run(router.project_action(go, _act("scrape", state), state, _pool(), None,
                                      JobManager()))
    assert "1. «Канал А»\n👤 @a · 🆔 <code>-1001</code>" in go.message.answers[-2][0]  # as on the card
    card = projects.card(projects.load()[0])
    assert "1. «Канал А»\n👤 @a · 🆔 -1001\n\n2. «Канал Б»\n🔗 t.me/b · 🆔 -1002" in card


def test_the_cli_s_project_scrape_names_the_channels_on_its_card(monkeypatch):
    from functions import scraper

    projects.add("Green", ["@a"])
    monkeypatch.setattr(scraper, "_lookup", lambda account, chats: [_info("@a", "«Канал А»", None, -1001, "a")])
    monkeypatch.setattr(scraper.console, "print", lambda *a, **k: None)
    assert scraper._chats_ok(_PICKED, ["@a"], "Green") == (True, ["«Канал А» (@a)"])
    assert projects.load()[0].chats["@a"]["title"] == "«Канал А»"


def test_the_bot_s_card_copies_ids_and_the_terminal_s_has_no_tags(tmp_path, monkeypatch):
    monkeypatch.setattr(scraped_files, "BASES_DIR", str(tmp_path))
    projects.add("A&B", ["-1001629147115", "@new"])
    projects.remember("A&B", [_info("-1001629147115", "«Берлога <1>»", None, -1001629147115)])
    [project] = projects.load()
    assert projects.card(project, html=True).startswith(
        "📁 A&amp;B\n\n📢 Каналы — 2\n\n1. «Берлога &lt;1&gt;»\n🆔 <code>-1001629147115</code>\n\n2. @new\n")
    assert "<code>" not in projects.card(project) and "&amp;" not in projects.card(project)

    class Msg:
        def __init__(self):
            self.sent = []

        async def answer(self, text, **kwargs):
            self.sent.append((text, kwargs.get("parse_mode")))

    msg = Msg()
    asyncio.run(_router()._show_card(msg, _State(), project))
    [(text, parse_mode)] = msg.sent
    assert parse_mode == "HTML" and "🆔 <code>-1001629147115</code>" in text


def test_the_cli_s_verify_from_a_card_names_the_project_s_channel(monkeypatch):
    from functions import scraper

    projects.add("Green", ["@a"])
    monkeypatch.setattr(scraper, "pick_session", lambda storage, personal=None: _PICKED)
    monkeypatch.setattr(scraper, "verify_run", lambda creds, params: None)
    monkeypatch.setattr(scraper, "_lookup", lambda account, chats: [_info("@a", "«Канал А»", None, -1001, "a")])
    monkeypatch.setattr(scraper.console, "print", lambda *a, **k: None)
    _answers(monkeypatch, scraper, texts=["posts.parquet", "@a", "01.01.2024", "31.01.2024", "", "0"])
    scraper.VerifyFunc(FakeStorage(), FakeSettings()).execute(posts=[], project="Green")
    assert projects.load()[0].chats["@a"] == {"title": "«Канал А»", "id": -1001, "username": "a"}


# --- 📥 Загрузить список: the project's list, a base for the mailing -----------------------------

def test_a_list_takes_usernames_links_and_numbers_once_each(tmp_path):
    added = projects.add_to_list("Green", ["@Anna_k", "https://t.me/bobby", "+7 (999) 123-45-67", "", "привет", "-",
                                           "anna_k", "79991234567"])
    assert added == (3, 3, 2)  # a blank line is no recipient; «ann» and the number again: the same people
    path = tmp_path / "Green_participants_list.txt"
    assert path.read_text(encoding="utf-8") == "@Anna_k\n@bobby\n+79991234567\n"
    assert projects.add_to_list("Green", ["@carlos", "@BOBBY"]) == (1, 4, 0)  # added to it, never in its place
    # the mailing takes it; the analysis and the contacts never see a text file
    assert scraped_files.project_files("Green", kinds=("participants", "list")) == [(str(path), "list · 4")]
    assert scraped_files.project_files("Green") == []


def test_a_project_s_list_follows_its_rename(tmp_path):
    projects.add("Green", [])
    projects.add_to_list("Green", ["@anna_k"])
    projects.rename("Green", "Blue")
    assert scraped_files.read_list(tmp_path / "Blue_participants_list.txt") == ["@anna_k"]


def test_bot_uploads_a_list_and_the_mailing_offers_it(tmp_path):
    from bot.routers import broadcasts
    from bot.states import Projects

    router = _router()
    projects.add("Clients", [])
    state = _State({"project_name": "Clients"})
    ask = _SCallback()
    asyncio.run(router.project_action(ask, _act("upload", state), state, _pool(), None, None))
    assert state.state == Projects.upload and "по одному @username" in ask.message.answers[0][0]

    pasted = _Msg(text="@anna_k\n@bobby\nне имя")
    asyncio.run(router.project_upload(pasted, state))
    note, card = pasted.answers[0][0], pasted.answers[1][0]
    assert note == "✅ В список проекта добавлено: 2, всего в нём: 2. Пропущено строк: 1 — не @username и не номер."
    assert "• list · 2" in card
    # a list has no IDs: the mailing, not the contacts, which say why
    contacts = _AlertCallback()
    asyncio.run(router.project_action(contacts, _act("contacts", state), state, _pool(), None, None))
    assert contacts.alerts[0][0].endswith("Загруженный список для контактов не подходит: в нём нет ID.")

    asyncio.run(router.project_action(_SCallback(), _act("upload", state), state, _pool(), None, None))
    sent = _Msg(document=ns(file_name="more.TXT", file_size=20), content="@bobby\n@carlos\n".encode())
    asyncio.run(router.project_upload(sent, state))
    assert sent.answers[0][0] == "✅ В список проекта добавлено: 1, всего в нём: 3."

    mail = _SCallback()
    asyncio.run(router.project_action(mail, _act("mail", state), state, _pool(), None, None))
    assert _buttons(mail.message.answers[0][1]) == ["📄 list · 3", "✖️ Отмена"]
    asyncio.run(broadcasts.mail_base(_SCallback(), ChoiceCB(scope="mail_base", value="0"), state))
    assert state.data["path"] == str(tmp_path / "Clients_participants_list.txt")


def test_bot_asks_again_for_a_list_with_no_one_in_it():
    router = _router()
    projects.add("Clients", [])
    state = _State({"project_name": "Clients"})
    nothing = _Msg(text="привет\n-")
    asyncio.run(router.project_upload(nothing, state))
    assert nothing.answers == [("Ни одного @username или номера в списке. Пришлите ещё раз:", None)]
    other = _Msg(document=ns(file_name="list.csv", file_size=20))
    asyncio.run(router.project_upload(other, state))
    assert other.answers == [("Нужен .txt файл или список текстом. Пришлите ещё раз:", None)]
    assert not scraped_files.list_path("Clients").exists()


def test_cli_uploads_a_list_and_mails_it(monkeypatch, tmp_path):
    from functions import pmmailing

    projects.add("Clients", [])
    source = tmp_path / "people.txt"
    source.write_text("@anna_k\nt.me/bobby\nне имя\n", encoding="utf-8")
    _, printed = _cli_card(monkeypatch, texts=["4", str(source)])
    assert "В список проекта добавлено: 2, всего в нём: 2. Пропущено строк: 1 — не @username и не номер." in printed

    offered = []

    async def execute(self, bases):
        offered.extend(bases)

    monkeypatch.setattr(pmmailing.PmMailingFunc, "execute", execute)
    result, printed = _cli_card(monkeypatch, texts=["8"])
    asyncio.run(result)
    assert offered == [(str(tmp_path / "Clients_participants_list.txt"), "list · 2")]


# --- a typed channel that can't be one is caught as it is typed -------------------------------------

def test_what_can_t_be_a_channel_is_caught():
    from scraper.scrape import bad_channels, parse_channels

    good = "@chan durov t.me/+AbC https://t.me/c/123/4 -1001629147115 telegram.me/x 123"
    assert bad_channels(parse_channels(good)) == []
    assert bad_channels(parse_channels("- — канал @x? 1abc @_a")) == ["-", "—", "канал", "@x?", "1abc", "@_a"]


def test_bot_asks_again_for_channels_that_can_t_be_ones():
    router = _router()
    state = _State({"new_name": "Green"})
    typed = _Msg(text="@a, -, канал")
    asyncio.run(router.project_channels(typed, state))
    assert typed.answers == [("Не похоже на канал: «-», «канал» — нужен @username, ссылка t.me или числовой id. "
                              "Пришлите список ещё раз:", None)]
    assert projects.load() == []

    projects.add("A", ["@a"])
    state = _State({"project_name": "A"})
    edited = _Msg(text="@b —")
    asyncio.run(router.project_edit(edited, state))
    assert edited.answers[0][0].startswith("Не похоже на канал: «—»") and projects.load()[0].channels == ["@a"]


def test_cli_asks_again_for_channels_that_can_t_be_ones(monkeypatch):
    from functions import projects as cli

    printed = []
    monkeypatch.setattr(cli.console, "print", lambda text="", *a, **k: printed.append(str(text)))
    monkeypatch.setattr(cli.TelethonFunction, "ask_int", staticmethod(lambda *a, **k: 1))  # [1] ➕ Новый
    _answers(monkeypatch, cli, texts=["Green", "-", "@a", ""])  # the dash: asked again
    cli.ProjectsFunc(FakeStorage(), FakeSettings()).execute()
    assert projects.load() == [projects.Project("Green", ["@a"])]
    assert any(line.startswith("Не похоже на канал: «-»") for line in printed)


def test_cli_channels_prompt_says_what_enter_does(monkeypatch):
    from functions import projects as cli

    asked = []

    def ask(prompt, *a, **k):
        asked.append(prompt)
        return "9" if len(asked) == 1 else ""

    projects.add("Green", ["@a"])
    monkeypatch.setattr(cli.TelethonFunction, "ask_int", staticmethod(lambda *a, **k: 1))
    monkeypatch.setattr(cli.console, "print", lambda *a, **k: None)
    monkeypatch.setattr(cli.Prompt, "ask", ask)
    cli.ProjectsFunc(FakeStorage(), FakeSettings()).execute()  # [9], Enter: its channels dropped
    assert "Enter — убрать все каналы" in asked[1] and projects.load()[0].channels == []

    asked.clear()
    cli.ProjectsFunc(FakeStorage(), FakeSettings()).execute()  # none left: Enter keeps it so
    assert "Enter — оставить без каналов" in asked[1]


# --- 🧹 Очистить: the bases only ever added to, moved out of the project's way -------------------------

def _members_base(tmp_path, name, rows=2):
    path = tmp_path / f"{name}_participants_members.parquet"
    pd.DataFrame({"ID": range(rows), "Access Hash": range(rows)}).to_parquet(path)
    return path


def test_clear_moves_the_file_to_previous_where_it_can_be_taken_back(tmp_path):
    projects.add_to_list("Green", ["@anna_k"])
    moved = projects.clear("Green", "list")
    assert moved == tmp_path / "Green_previous" / "Green_participants_list.txt"
    assert scraped_files.read_list(moved) == ["@anna_k"]
    assert scraped_files.project_files("Green", kinds=("participants", "list")) == []
    projects.add_to_list("Green", ["@bobby"])
    projects.clear("Green", "list")  # cleared again: the one moved before is replaced, nothing else
    assert scraped_files.read_list(moved) == ["@bobby"]
    with pytest.raises(ValueError, match="нечего"):
        projects.clear("Green", "list")


def test_bot_clears_a_list_after_its_confirmation(tmp_path):
    router = _router()
    projects.add("Clients", [])
    state = _State({"project_name": "Clients"})
    manage = _SCallback()
    asyncio.run(router.project_action(manage, _act("manage", state), state, _pool(), None, None))
    assert "🧹 Очистить базу" not in _buttons(manage.message.edits[0][1])  # nothing to clear yet

    _members_base(tmp_path, "Clients", rows=3)
    projects.add_to_list("Clients", ["@anna_k", "@bobby"])
    ask = _SCallback()
    asyncio.run(router.project_action(ask, _act("clear", state), state, _pool(), None, None))
    text, offer = ask.message.edits[0]  # in ⚙️'s place
    assert "Clients_previous/" in text and _buttons(offer) == ["🧹 Участники групп · 3", "🧹 Список · 2",
                                                               "↩️ Отмена"]

    def tap(label):  # a button of the confirmation shown last
        return ChoiceCB.unpack(next(b for row in offer.inline_keyboard for b in row if b.text == label).callback_data)

    back = _SCallback()
    asyncio.run(router.project_clear(back, tap("↩️ Отмена"), state, JobManager()))
    assert back.message.edits[0][0].startswith("⚙️ <b>Управление проектом «Clients»</b>")
    assert scraped_files.list_path("Clients").exists()

    asyncio.run(router.project_action(ask, _act("clear", state), state, _pool(), None, None))
    offer = ask.message.edits[-1][1]
    done = _SCallback()
    asyncio.run(router.project_clear(done, tap("🧹 Список · 2"), state, JobManager()))
    text, markup = done.message.edits[0]
    assert text.startswith("✅ Очищено: список. Файл — в ") and "Clients_previous" in text
    assert not scraped_files.list_path("Clients").exists() and scraped_files.members_base("Clients").exists()
    assert text.endswith(f"<i>{RISK_NOTE}</i>")  # the card, under the note

    stale = _SCallback()  # the confirmation's button once another card is shown: nothing cleared
    asyncio.run(router.project_clear(stale, tap("🧹 Участники групп · 3"), state, JobManager()))
    assert stale.message.answers[0][0].startswith("Кнопка устарела") and scraped_files.members_base("Clients").exists()
    manage = _SCallback()
    asyncio.run(router.project_action(manage, _act("manage", state), state, _pool(), None, None))
    text, markup = manage.message.edits[0]
    assert "🧹 Очистить базу" in _buttons(markup)  # the members' base is left
    assert "<b>🧹 Очистить базу</b> — участники групп или загруженный список уходят из проекта в " \
           "&lt;имя&gt;_previous/, оттуда их можно вернуть" in text


def test_bot_wont_clear_while_members_are_collected(tmp_path):
    router = _router()
    projects.add("Clients", [])
    _members_base(tmp_path, "Clients")
    state = _State({"project_name": "Clients"})
    ask = _SCallback()
    asyncio.run(router.project_action(ask, _act("clear", state), state, _pool(), None, None))
    members = ChoiceCB.unpack(ask.message.edits[0][1].inline_keyboard[0][0].callback_data)
    scrapes = JobManager()
    scrapes.acquire("Участники чата", cancelable=False, timeout=0)
    busy = _SCallback()
    asyncio.run(router.project_clear(busy, members, state, scrapes))
    assert busy.message.answers == [("⛔ Идёт «Участники чата» — очистите после её окончания.", None)]
    assert scraped_files.members_base("Clients").exists()


def test_cli_clears_after_its_confirmation(monkeypatch, tmp_path):
    from functions import projects as cli

    projects.add("Clients", [])
    _members_base(tmp_path, "Clients")
    printed = []
    monkeypatch.setattr(cli.TelethonFunction, "ask_int", staticmethod(lambda *a, **k: 1))  # the project, its base
    monkeypatch.setattr(cli.console, "print", lambda text="", *a, **k: printed.append(str(text)))
    _answers(monkeypatch, cli, texts=["11"], bools=[False])  # 🧹, then no
    cli.ProjectsFunc(FakeStorage(), FakeSettings()).execute()
    assert scraped_files.members_base("Clients").exists()

    _answers(monkeypatch, cli, texts=["11"], bools=[True])
    cli.ProjectsFunc(FakeStorage(), FakeSettings()).execute()
    assert not scraped_files.members_base("Clients").exists()
    assert any(line.startswith("Очищено: участники групп.") for line in printed)


# --- the chats by name everywhere, as on the card ----------------------------------------------------

def test_the_channels_prompt_shows_them_by_name_and_as_a_list_to_copy():
    router = _router()
    projects.add("Green", ["-1001629147115", "@b"])
    projects.remember("Green", [_info("-1001629147115", "«Берлога»", None, -1001629147115)])
    state = _State({"project_name": "Green"})
    ask = _SCallback()
    asyncio.run(router.project_action(ask, _act("channels", state), state, _pool(), None, None))
    text = ask.message.answers[0][0]
    assert text.startswith("Сейчас:\n\n1. «Берлога»\n🆔 <code>-1001629147115</code>\n\n2. @b\n\n")
    assert "Списком (нажмите, чтобы скопировать): <code>-1001629147115, @b</code>" in text


def test_a_chat_list_is_kept_within_its_limit():
    project = projects.Project("Green", [])
    raws = [f"@chan{i}" for i in range(25)]
    listed = projects.chat_list(project, raws)
    assert listed.startswith("1. @chan0\n\n2. @chan1") and listed.endswith("20. @chan19\n\n… и ещё 5")
    assert projects.chat_list(project, raws, budget=30) == "1. @chan0\n\n2. @chan1\n\n… и ещё 23"  # whole blocks
    assert projects.chat_list(None, ["@x"], problems={"@x": "нет такого"}) == "1. @x\n⚠️ нет такого"


def test_the_groups_of_members_get_named_on_the_project(monkeypatch):
    from bot.routers import scraping
    from scraper.scrape import ChatInfo

    async def lookup(message, pool, personal, account, chats):
        return [ChatInfo("@grp", "«Группа»", None, -1005, "grp")]

    monkeypatch.setattr(scraping, "_lookup", lookup)
    projects.add("Green", [])
    state = _State({"only_project": "Green", "account": "sessions/w.jsession"})
    went = []

    async def ready(message, state, deps):
        went.append(True)

    monkeypatch.setattr(scraping, "_members_ready", ready)
    asyncio.run(scraping.members_chats(_SMsg(text="@grp"), state, _pool(), None, None))
    assert went and projects.find("Green").chat("@grp").title == "«Группа»"
