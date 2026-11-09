"""📚 Вся база: a project's bases glued, each person once (modules/project_base.py), and where the bot takes it — the
mailing, 📇 В контакты, 🎯's count, 📦 Скачать базу."""
import asyncio
import csv
import os
from types import SimpleNamespace as ns

import pandas as pd

from bot.callbacks import ChoiceCB
from bot.routers import audience, mailing
from modules import project_base, scraped_files


def _base(folder, name, rows, mtime):
    """A participants base: rows of (id, hash, owner, username)."""
    path = folder / f"{name}.parquet"
    pd.DataFrame({"ID": pd.array([r[0] for r in rows], dtype="Int64"),
                  "Access Hash": pd.array([r[1] for r in rows], dtype="Int64"),
                  "Owner ID": pd.array([r[2] for r in rows], dtype="Int64"),
                  "Username": [r[3] for r in rows], "Name": ["x"] * len(rows)}).to_parquet(path)
    os.utime(path, (mtime, mtime))
    return str(path)


def _green(tmp_path, monkeypatch):
    """Green: a scrape's base (by account 900), a newer members base (by its worker 7), its list."""
    monkeypatch.setattr(scraped_files, "BASES_DIR", str(tmp_path))
    _base(tmp_path, "Green_participants_01.01.2024-31.01.2024",
          [(1, 11, 900, "Anna"), (2, 21, 900, None), (3, 31, 900, "carl")], 1_000)
    _base(tmp_path, "Green_participants_members",
          [(1, 12, 7, None), (2, 22, 900, None), (4, 41, None, "dora")], 2_000)
    (tmp_path / "Green_participants_list.txt").write_text("@anna\n@eve\n+79990000000\n@CARL\n")
    _base(tmp_path, "Other_participants_members", [(9, 91, 7, "other")], 3_000)  # another project's


def test_each_person_once_the_row_a_worker_of_the_project_can_use(tmp_path, monkeypatch):
    _green(tmp_path, monkeypatch)
    found = project_base.merged("Green", owners={7})
    rows = {row["user_id"]: row for row in found if isinstance(row, dict)}
    assert sorted(rows) == [1, 2, 3, 4]  # 1 and 2 in both bases: once; Other's 9 not
    assert rows[1]["access_hash"] == 12 and rows[1]["owner_id"] == 7  # its worker's hash
    assert rows[1]["username"] == "Anna"  # from the other row: a worker the hash isn't for can still reach her
    assert rows[2]["access_hash"] == 22  # one rank in both: the newer file's
    assert rows[4]["owner_id"] is None and rows[4]["access_hash"] == 41
    # the list's @anna and @CARL are rows already (any case): only the others are left, after the rows
    assert [person for person in found if isinstance(person, str)] == ["@eve", "+79990000000"]
    assert project_base.people("Green") == 6


def test_a_known_owner_comes_before_another_account_s(tmp_path, monkeypatch):
    monkeypatch.setattr(scraped_files, "BASES_DIR", str(tmp_path))
    _base(tmp_path, "Blue_participants_a", [(1, 11, None, None)], 1_000)
    _base(tmp_path, "Blue_participants_b", [(1, 12, 900, None)], 2_000)  # newer, but another account's hash
    [row] = project_base.merged("Blue")
    assert row["access_hash"] == 11


def test_a_file_that_is_no_base_is_left_out(tmp_path, monkeypatch):
    monkeypatch.setattr(scraped_files, "BASES_DIR", str(tmp_path))
    pd.DataFrame({"a": [1, 2]}).to_parquet(tmp_path / "Blue_participants_x.parquet")
    _base(tmp_path, "Blue_participants_y", [(1, 11, None, None)], 1_000)
    assert len(project_base.merged("Blue")) == 1


def test_the_export_is_the_whole_base_as_a_csv(tmp_path, monkeypatch):
    _green(tmp_path, monkeypatch)
    path = tmp_path / "out.csv"
    assert project_base.export("Green", str(path)) == 6
    with open(path, encoding="utf-8-sig") as fileobj:
        rows = list(csv.DictReader(fileobj))
    assert list(rows[0]) == list(project_base.EXPORT_COLUMNS)
    assert {row["user_id"] for row in rows} == {"1", "2", "3", "4", ""}
    assert [(row["username"], row["phone"]) for row in rows if not row["user_id"]] == [("@eve", ""),
                                                                                     ("", "+79990000000")]


# --- the bot ---

class _State:
    def __init__(self, data=None):
        self.data, self.state = dict(data or {}), None

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kw):
        self.data.update(kw)

    async def set_state(self, state):
        self.state = state

    async def get_state(self):
        return self.state

    async def clear(self):
        self.data, self.state = {}, None


class _Msg:
    def __init__(self):
        self.answers, self.chat, self.bot = [], ns(id=1), None

    async def answer(self, text, **kwargs):
        self.answers.append(text)

    async def edit_text(self, text, **kwargs):
        self.answers.append(text)

    async def edit_reply_markup(self, **kwargs):
        pass


class _Callback:
    def __init__(self):
        self.message, self.alerts = _Msg(), []

    async def answer(self, text=None, **kwargs):
        if text:
            self.alerts.append(text)


def _pool():
    return ns(storage=ns(jsessions_paths={}, home=None))


def test_the_mailing_s_whole_base_is_read_when_it_runs(tmp_path, monkeypatch):
    _green(tmp_path, monkeypatch)
    state = _State()
    asyncio.run(mailing.ask_recipients(_Msg(), state, "Green", 6, [("f", "x")]))
    asyncio.run(mailing.mail_base(_Callback(), ChoiceCB(scope="mail_base", value=project_base.WHOLE), state))
    assert state.data["path"] == project_base.WHOLE and state.data["project"] == "Green"

    stale = _Callback()  # 📚 of a flow gone: no project to glue
    asyncio.run(mailing.mail_base(stale, ChoiceCB(scope="mail_base", value=project_base.WHOLE), _State()))
    assert stale.alerts == ["Кнопка устарела — откройте раздел заново."]


def test_the_contacts_whole_base_is_its_people_with_an_id(tmp_path, monkeypatch):
    _green(tmp_path, monkeypatch)
    launched = []

    async def launch(message, source, *rest):
        launched.append(source)
    monkeypatch.setattr(audience, "_launch", launch)
    state = _State()
    asyncio.run(audience.ask_base(_Msg(), state, "Green", 4, [("f", "x")]))
    asyncio.run(audience.run_base(_Callback(), ChoiceCB(scope="contacts_base", value=project_base.WHOLE), state,
                                  _pool(), {}, ns(active=False)))
    [rows] = launched
    assert sorted(row["user_id"] for row in rows) == [1, 2, 3, 4]  # the list's lines have no id


def test_the_contacts_whole_base_unread_is_said_as_the_mailing_s(tmp_path, monkeypatch):
    def unreadable(*args):
        raise PermissionError("Green_participants_list.txt")
    monkeypatch.setattr(project_base, "merged", unreadable)
    state, callback = _State(), _Callback()
    asyncio.run(audience.ask_base(_Msg(), state, "Green", 4, [("f", "x")]))
    asyncio.run(audience.run_base(callback, ChoiceCB(scope="contacts_base", value=project_base.WHOLE), state,
                                  _pool(), {}, ns(active=False, project_id=1)))
    assert callback.message.answers[-1] == "⚠️ База не прочитана: Green_participants_list.txt"


def test_a_mailing_of_the_whole_base_runs_on_the_glued_people(tmp_path, monkeypatch):
    from functions.pmmailing import PmMailingFunc

    _green(tmp_path, monkeypatch)
    captured = {}

    class _Inst(PmMailingFunc):
        def __init__(self):
            pass

        def load_stats(self):
            self.stats = {"3": {"count": 1}}  # carl was written to before

        async def run(self, recipients, content, reporter, run_id=None):
            captured["recipients"] = recipients

    class _Manager:
        active, label, project_id = False, "", 1

        async def run(self, bot, chat_id, pool, instance, bot_function, job, header, done=None, **kw):
            await job(instance, lambda text: asyncio.sleep(0))
            return True

    async def content(message, album):
        return ns(cleanup=lambda: None)
    monkeypatch.setattr(mailing, "resolve", lambda functions, key: (_Inst(), ns(risk="risky")))
    monkeypatch.setattr(mailing, "build_content", content)
    state = _State({"path": project_base.WHOLE, "project": "Green", "skip": True, "limit": None})
    asyncio.run(mailing.mail_run(_Msg(), state, None, _pool(), {}, _Manager(), ns(delay=[0])))
    people = captured["recipients"]
    assert sorted(person["user_id"] for person in people if isinstance(person, dict)) == [1, 2, 4]  # carl: mailed
    assert [person for person in people if isinstance(person, str)] == ["@eve", "+79990000000"]


def test_the_base_s_download_is_one_csv_under_its_screen(tmp_path, monkeypatch):
    from bot.routers.projects import card

    _green(tmp_path, monkeypatch)
    sent = []

    class _Doc(_Msg):
        async def answer_document(self, document, caption=None, **kwargs):
            sent.append((open(document.path, encoding="utf-8-sig").read().splitlines()[0], caption))

    asyncio.run(card._export(ns(project=ns(name="Green"), message=_Doc())))
    assert sent == [(",".join(project_base.EXPORT_COLUMNS), "📦 База проекта «Green»: людей 6, без повторов.")]


def test_the_count_is_read_again_only_once_the_base_s_files_change(tmp_path, monkeypatch):
    _green(tmp_path, monkeypatch)
    reads = []
    merged = project_base.merged
    monkeypatch.setattr(project_base, "merged", lambda *a, **k: reads.append(1) or merged(*a, **k))
    assert (project_base.people("Green"), project_base.people("Green", with_id=True)) == (6, 4)
    assert project_base.people("Green") == 6 and len(reads) == 1  # the card opened again: no file read
    (tmp_path / "Green_participants_list.txt").write_text("@anna\n@eve\n+79990000000\n@CARL\n@zoe\n")
    assert project_base.people("Green") == 7 and len(reads) == 2
