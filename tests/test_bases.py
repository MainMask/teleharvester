"""Offline tests for picking a scraped recipient base by button (pmmailing / addcontacts)."""

import asyncio
import os
import types

import pandas as pd

from bot.callbacks import ChoiceCB
from bot.routers import audience, broadcasts
from bot.routers import scraping as scraping_router
from bot.services import scraping
from modules import scraped_files


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
    def __init__(self):
        self.answers = []
        self.bot, self.chat = object(), ns(id=1)

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs.get("reply_markup")))


class _Callback:
    def __init__(self):
        self.message = _Msg()

    async def answer(self, *args, **kwargs):
        pass


def _base(dir_, name, rows, mtime):
    path = dir_ / name
    pd.DataFrame({"ID": range(rows), "Access Hash": range(rows)}).to_parquet(path)
    os.utime(path, (mtime, mtime))
    return str(path)


def _bases_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(scraped_files, "BASES_DIR", str(tmp_path))
    old = _base(tmp_path, "Old_participants_01.01.2026-31.01.2026.parquet", 3, 1_000)
    new = _base(tmp_path, "OmniBase_participants_01.05.2022-27.09.2026.parquet", 1234, 2_000)
    _base(tmp_path, "OmniBase_posts_01.05.2022-27.09.2026.parquet", 5, 3_000)  # not a base
    return old, new


def test_bases_newest_first_with_row_counts(tmp_path, monkeypatch):
    old, new = _bases_dir(tmp_path, monkeypatch)

    assert scraped_files.participant_bases() == [
        (new, "OmniBase · 01.05.2022-27.09.2026 · 1 234"),
        (old, "Old · 01.01.2026-31.01.2026 · 3"),
    ]


def test_no_bases_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(scraped_files, "BASES_DIR", str(tmp_path / "missing"))
    assert scraped_files.participant_bases() == []


def _buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


def test_mailing_offers_bases_and_takes_the_picked_one(tmp_path, monkeypatch):
    old, new = _bases_dir(tmp_path, monkeypatch)
    state, callback = _State(), _Callback()
    pool = ns(count=lambda: 1, workers=[object()])

    asyncio.run(broadcasts.mail_start(callback, state, pool))
    _, markup = callback.message.answers[0]
    assert [b.text for b in _buttons(markup)][0].startswith("OmniBase")
    assert state.data["bases"] == [new, old]

    pick = _Callback()
    asyncio.run(broadcasts.mail_base(pick, ChoiceCB(scope="mail_base", value="1"), state))
    assert state.data["path"] == old
    assert pick.message.answers[0][0] == "Пропускать уже отправленных?"


def test_mailing_without_bases_has_no_keyboard(tmp_path, monkeypatch):
    monkeypatch.setattr(scraped_files, "BASES_DIR", str(tmp_path))
    callback = _Callback()
    asyncio.run(broadcasts.mail_start(callback, _State(), ns(count=lambda: 1, workers=[object()])))
    text, markup = callback.message.answers[0]
    assert markup is None and "выберите базу" not in text


def test_contacts_button_launches_with_the_picked_base(tmp_path, monkeypatch):
    _, new = _bases_dir(tmp_path, monkeypatch)
    state = _State({"bases": [new]})
    runs = []

    class _Manager:
        async def run(self, bot, chat_id, pool, instance, bot_function, job, *labels):
            calls = []

            async def fake_run(path, delay, report):
                calls.append(path)

            await job(ns(run=fake_run), None)
            runs.append(calls[0])

    functions = {"AddContactsFunc": object()}
    asyncio.run(audience.run_base(
        _Callback(), ChoiceCB(scope="contacts_base", value="0"), state,
        ns(), functions, _Manager(), ns(delay=[1]),
    ))
    assert runs == [new]
    assert state.data == {}  # flow finished


# --- base owner: only the scraping account can use the base's access hashes -------------

OWNER, OTHER = 100, 200


class _Session:
    """A worker: records what it was asked to message / add."""

    def __init__(self, uid, path=None):
        self.uid, self.path, self.sent, self.added = uid, path, [], []

    async def get_me(self):
        return ns(id=self.uid, first_name=f"w{self.uid}")

    async def get_input_entity(self, username):
        return ns(username=username)

    async def __call__(self, request):
        self.added.append(request.id)


class _WorkerStorage:
    def __init__(self, sessions):
        self.sessions = sessions
        self.jsessions_paths = {
            s.path: ns(account=ns(account=ns(user_id=s.uid, phone_number=str(s.uid), username=None,
                                             first_name=f"w{s.uid}")))
            for s in sessions if s.path
        }

    def get_session_path(self, session):
        return session.path

    def ainitialize_session(self, session):
        import contextlib

        @contextlib.asynccontextmanager
        async def cm():
            yield

        return cm()


ROWS = [
    {"user_id": 1, "access_hash": 11, "username": "alice", "owner_id": OWNER},
    {"user_id": 2, "access_hash": 22, "username": None, "owner_id": OWNER},
]


def _mailing(sessions, rows, monkeypatch, tmp_path, storage=None, on_hold=()):
    from functions import pmmailing

    monkeypatch.chdir(tmp_path)
    fn = pmmailing.PmMailingFunc(storage or _WorkerStorage(sessions), ns(
        delay=[0], per_account_daily=100, account_pause=[0]))
    fn.sessions = list(sessions)
    fn.on_hold = list(on_hold)
    delays = []

    async def delay():
        delays.append(1)

    fn.delay = delay

    async def deliver(session, peer):
        session.sent.append(peer)

    fn._deliver = deliver
    msgs = []

    async def report(text):
        msgs.append(text)

    asyncio.run(fn.run(rows, None, [0], report))
    return msgs, delays


def test_mailing_foreign_base_uses_username_and_skips_the_rest(monkeypatch, tmp_path):
    other = _Session(OTHER)
    # the skipped row first: a real send would be followed by a delay, a skip must not be
    msgs, delays = _mailing([other], ROWS[::-1], monkeypatch, tmp_path)

    assert other.sent == ["alice"]  # straight by username, no attempt with the foreign hash
    assert "Пропущено без username (база другого аккаунта): 1" in msgs
    assert not any("not sent" in m for m in msgs)  # the skip is silent
    assert delays == []  # nothing waits after a skipped row


def test_mailing_owner_uses_hash_for_everyone(monkeypatch, tmp_path):
    from telethon import types as tl

    owner = _Session(OWNER)
    msgs, _ = _mailing([owner], ROWS, monkeypatch, tmp_path)

    assert all(isinstance(p, tl.InputPeerUser) for p in owner.sent) and len(owner.sent) == 2
    assert not any("Пропущено" in m for m in msgs)


def test_mailing_owner_takes_only_people_without_username(monkeypatch, tmp_path):
    from telethon import types as tl

    other, owner = _Session(OTHER, "a.jsession"), _Session(OWNER, "b.jsession")
    _mailing([other, owner], ROWS, monkeypatch, tmp_path)

    assert [p.user_id for p in owner.sent] == [2] and isinstance(owner.sent[0], tl.InputPeerUser)
    assert other.sent == ["alice"]  # people with a username go to the other workers


def test_mailing_base_without_owner_keeps_old_behaviour(monkeypatch, tmp_path):
    from telethon import types as tl

    rows = [{"user_id": 1, "access_hash": 11, "username": "alice"}]
    other = _Session(OTHER)
    _mailing([other], rows, monkeypatch, tmp_path)
    assert isinstance(other.sent[0], tl.InputPeerUser)


def test_split_queues_by_owner():
    from functions.pmmailing import PmMailingFunc

    a, b = _Session(1, "a"), _Session(OWNER, "b")
    fn = PmMailingFunc(_WorkerStorage([a, b]), ns(delay=[0]))
    fn.sessions = [a, b]

    assert fn.split_queues(ROWS) == [([b], [ROWS[1]]), ([a, b], [ROWS[0]])]  # the owner last
    unknown = [dict(r, owner_id=999) for r in ROWS]
    assert fn.split_queues(unknown) == [([a, b], unknown)]  # owner isn't a worker
    assert fn.split_queues(["@x"]) == [([a, b], ["@x"])]    # a .txt list has no owner


def test_split_queues_contacts_ledger_wins():
    from functions.pmmailing import PmMailingFunc

    a, b, c = _Session(1, "a"), _Session(2, "b"), _Session(OWNER, "c")
    fn = PmMailingFunc(_WorkerStorage([a, b, c]), ns(delay=[0]))
    fn.sessions = [a, b, c]
    rows = [dict(ROWS[0], user_id=10), dict(ROWS[1], user_id=20), dict(ROWS[0], user_id=30),
            dict(ROWS[0], user_id=40)]
    # 10 -> b's contact; 20 (no username) -> owner; 30 -> a removed worker: shared; 40 -> shared
    queues = fn.split_queues(rows, {"10": 2, "30": 555})

    assert queues == [([b], [rows[0]]), ([c], [rows[1]]), ([a, b, c], [rows[2], rows[3]])]


def test_contacts_foreign_base(monkeypatch, tmp_path):
    from functions.add_contacts import AddContactsFunc

    path = tmp_path / "base.parquet"
    pd.DataFrame({"ID": [1, 2], "Access Hash": [11, 22], "Username": ["alice", None],
                  "Owner ID": [OWNER, OWNER]}).to_parquet(path)
    other = _Session(OTHER)
    fn = AddContactsFunc(_WorkerStorage([other]), ns(delay=[0], contacts_per_account_daily=0))
    fn.sessions = [other]
    msgs = []

    async def report(text):
        msgs.append(text)

    asyncio.run(fn.run(str(path), [0], report))
    assert [u.username for u in other.added] == ["alice"]
    assert "Пропущено без username (база другого аккаунта): 1" in msgs


# --- contacts ledger: each person in one worker's contacts; the mailing routes them there ----

def _contacts(sessions, rows, tmp_path, storage=None, progress=None, on_hold=()):
    from functions.add_contacts import AddContactsFunc

    path = tmp_path / "base.parquet"
    pd.DataFrame(rows).to_parquet(path)
    fn = AddContactsFunc(storage or _WorkerStorage(sessions), ns(delay=[0], contacts_per_account_daily=0))
    fn.sessions = list(sessions)
    fn.on_hold = list(on_hold)
    fn.progress = progress
    msgs = []

    async def report(text):
        msgs.append(text)

    asyncio.run(fn.run(str(path), [0], report))
    return msgs


def test_contacts_ledger_records_and_skips_on_rerun(tmp_path):
    from modules import contacts_ledger

    w = _Session(1, "w")
    rows = [{"ID": 10, "Access Hash": 1, "Username": "a"}, {"ID": 20, "Access Hash": 2, "Username": "b"}]
    _contacts([w], rows, tmp_path)
    assert contacts_ledger.load() == {"10": 1, "20": 1}

    w.added.clear()
    msgs = _contacts([w], rows, tmp_path)
    assert w.added == []  # nobody re-added
    assert "Уже в контактах (пропущено): 2" in msgs


def test_contacts_of_a_removed_worker_are_added_again(tmp_path):
    from modules import contacts_ledger

    contacts_ledger.save({"10": 555})  # 555 is no longer a worker
    w = _Session(1, "w")
    _contacts([w], [{"ID": 10, "Access Hash": 1, "Username": "a"}], tmp_path)

    assert len(w.added) == 1 and contacts_ledger.load() == {"10": 1}


def test_mailing_sends_to_a_contact_only_from_its_worker(monkeypatch, tmp_path):
    from modules import contacts_ledger

    a, b = _Session(1, "a"), _Session(2, "b")
    contacts_ledger.save({"10": 2})
    rows = [{"user_id": 10, "access_hash": 1, "username": "zed"}]
    _mailing([a, b], rows, monkeypatch, tmp_path)

    assert b.sent and a.sent == []


def test_a_limited_worker_s_contacts_wait_for_it(monkeypatch, tmp_path):
    from modules import contacts_ledger

    a, b = _Session(1, "a"), _Session(2, "b")
    contacts_ledger.save({"10": 2})
    rows = [{"user_id": 10, "access_hash": 1, "username": "zed"}]
    monkeypatch.setattr("modules.account_limits.DailyCounter.reached",
                        lambda self, key: key == "2")  # b is out for today
    msgs, _ = _mailing([a, b], rows, monkeypatch, tmp_path)

    assert a.sent == [] and b.sent == []  # a doesn't take b's contact
    assert "1 получателей ждут своего воркера до следующего запуска." in msgs
    assert not any("аккаунты исчерпаны" in m for m in msgs)  # waiting for tomorrow, not lost


def test_a_permanently_restricted_worker_s_contacts_wait_for_the_admin(monkeypatch, tmp_path):
    from modules import contacts_ledger, restricted_workers

    a, b = _Session(1, "a"), _Session(2, "b")
    monkeypatch.chdir(tmp_path)
    contacts_ledger.save({"10": 2})
    restricted_workers.save(["b"])
    rows = [{"user_id": 10, "access_hash": 1, "username": "zed"}]
    msgs, _ = _mailing([a, b], rows, monkeypatch, tmp_path)

    assert a.sent == [] and b.sent == []  # a misread @SpamBot reply must not hand them to a stranger
    assert "Пропущено бессрочно ограниченных воркеров: 1" in msgs
    assert "1 получателей ждут своего воркера: он не участвует в этом запуске." in msgs


def test_a_permanently_restricted_worker_s_contacts_go_to_others_once_released(monkeypatch, tmp_path):
    from modules import contacts_ledger, restricted_workers

    a, b = _Session(1, "a"), _Session(2, "b")
    monkeypatch.chdir(tmp_path)
    contacts_ledger.save({"10": 2})
    restricted_workers.save(["b"])
    restricted_workers.save_released(["b"])
    rows = [{"user_id": 10, "access_hash": 1, "username": "zed"}]
    msgs, _ = _mailing([a, b], rows, monkeypatch, tmp_path)

    assert a.sent and b.sent == []
    assert "Пропущено бессрочно ограниченных воркеров: 1" in msgs


def test_a_permanently_restricted_worker_s_contacts_are_not_added_again_until_released(monkeypatch, tmp_path):
    from modules import contacts_ledger, restricted_workers

    a, b = _Session(1, "a"), _Session(2, "b")
    monkeypatch.chdir(tmp_path)
    contacts_ledger.save({"10": 2})
    restricted_workers.save(["b"])
    rows = [{"ID": 10, "Access Hash": 1, "Username": "z"}]
    _contacts([a, b], rows, tmp_path)
    assert a.added == [] and contacts_ledger.load() == {"10": 2}

    restricted_workers.save_released(["b"])
    _contacts([a, b], rows, tmp_path)
    assert len(a.added) == 1 and b.added == [] and contacts_ledger.load() == {"10": 1}


def test_stats_are_keyed_by_id_but_reports_show_username(monkeypatch, tmp_path):
    import json

    w = _Session(OWNER)
    msgs, _ = _mailing([w], [ROWS[0]], monkeypatch, tmp_path)

    assert list(json.load(open(tmp_path / "stats" / "pm_mailing.json"))) == ["1"]
    assert any("sent" in m and "alice" in m for m in msgs)

    from functions.pmmailing import PmMailingFunc
    fn = PmMailingFunc(_WorkerStorage([w]), ns(delay=[0]))
    fn.load_stats()
    assert fn.filter_unsent([dict(ROWS[0], username="renamed")]) == []  # same id: already sent


# --- preflight: mailing / adding to contacts ask @SpamBot about their workers first ----------

CLEAN = "Good news, no limits are currently applied."
FOREVER = "Unfortunately...\nyou are limited forever."
UNTIL = "Unfortunately...\nyou are limited until 1 Nov 2026."


class _CheckedSession(_Session):
    """A worker @SpamBot can be asked about; me=None is a banned / logged-out account."""

    def __init__(self, uid, tmp_path, reply=CLEAN, dead=False):
        super().__init__(uid, str(tmp_path / "sessions" / f"w{uid}.jsession"))
        self.reply, self.dead = reply, dead
        self.session = ns(_entities=set())

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def get_me(self):
        return None if self.dead else ns(id=self.uid, username=f"w{self.uid}", first_name="w", last_name=None)

    def conversation(self, *_):
        reply = self.reply

        class _Conv:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def send_message(self, text):
                pass

            async def get_response(self):
                return ns(message=reply)

        return _Conv()


def _checked_storage(monkeypatch, tmp_path, sessions):
    import conftest
    from functions.base.base import BaseFunction
    from modules.storages.sessions_storage import SessionsStorage

    monkeypatch.setattr(BaseFunction, "check_workers", conftest.REAL_CHECK_WORKERS)
    (tmp_path / "sessions").mkdir(exist_ok=True)
    storage = SessionsStorage(str(tmp_path / "sessions"), 1, "x", initialize=False)
    for s in sessions:
        open(s.path, "w").write("x")
        storage.full_sessions[s.path] = s
        storage.jsessions_paths[s.path] = ns(account=ns(
            account=ns(user_id=s.uid, username=None, first_name="w", last_name=None),
            save=lambda path: None))
    return storage


def test_preflight_excludes_a_permanently_restricted_worker(monkeypatch, tmp_path):
    from modules import contacts_ledger

    a, b = _CheckedSession(1, tmp_path), _CheckedSession(2, tmp_path, reply=FOREVER)
    contacts_ledger.save({"10": 2})
    rows = [{"user_id": 10, "access_hash": 1, "username": "zed"}]
    storage = _checked_storage(monkeypatch, tmp_path, [a, b])
    msgs, _ = _mailing([a, b], rows, monkeypatch, tmp_path, storage)

    assert a.sent == [] and b.sent == []  # b's contact waits for the admin's decision
    assert "Пропущено бессрочно ограниченных воркеров: 1" in msgs
    assert any("его контакты (1) ждут решения" in m for m in msgs)
    assert ("Контакты бессрочно ограниченных воркеров ждут решения (1 чел.): "
            "🤖 Воркеры → 🩺 Проверка и статистика → Проверка статуса") in msgs
    assert not any(m.startswith("✅") for m in msgs)


def test_preflight_holds_a_worker_restricted_until_a_date(monkeypatch, tmp_path):
    from modules import contacts_ledger, restricted_workers

    a, b = _CheckedSession(1, tmp_path), _CheckedSession(2, tmp_path, reply=UNTIL)
    contacts_ledger.save({"10": 2})
    rows = [{"user_id": 10, "access_hash": 1, "username": "zed"},
            {"user_id": 11, "access_hash": 1, "username": "yan"}]
    storage = _checked_storage(monkeypatch, tmp_path, [a, b])
    msgs, _ = _mailing([b, a], rows, monkeypatch, tmp_path, storage)  # b would go first

    assert b.sent == [] and len(a.sent) == 1  # 11 from a; 10 waits for b
    assert "Пропущено воркеров, ограниченных до даты: 1" in msgs
    assert "1 получателей ждут своего воркера: он не участвует в этом запуске." in msgs
    assert restricted_workers.load() == []  # checked again next run


def test_preflight_moves_a_dead_worker_out(monkeypatch, tmp_path):
    a, b = _CheckedSession(1, tmp_path), _CheckedSession(2, tmp_path, dead=True)
    rows = [{"user_id": 10, "access_hash": 1, "username": "zed"}]
    storage = _checked_storage(monkeypatch, tmp_path, [b, a])  # the dead one would go first
    _mailing([b, a], rows, monkeypatch, tmp_path, storage)

    assert a.sent and b.sent == []
    assert (tmp_path / "sessions" / "inactive" / "w2.jsession").exists()
    assert storage.sessions == [a]


def test_preflight_brings_a_clean_worker_back(monkeypatch, tmp_path):
    from modules import contacts_ledger, restricted_workers

    a, b = _CheckedSession(1, tmp_path), _CheckedSession(2, tmp_path)
    contacts_ledger.save({"10": 2})
    restricted_workers.save([b.path])  # restricted last time, clean now
    rows = [{"user_id": 10, "access_hash": 1, "username": "zed"}]
    storage = _checked_storage(monkeypatch, tmp_path, [a, b])
    msgs, _ = _mailing([a, b], rows, monkeypatch, tmp_path, storage)

    assert b.sent and a.sent == []
    assert restricted_workers.load() == []
    assert not any("Пропущено бессрочно" in m for m in msgs)


def test_preflight_before_adding_to_contacts(monkeypatch, tmp_path):
    from modules import contacts_ledger

    a, b = _CheckedSession(1, tmp_path), _CheckedSession(2, tmp_path, reply=FOREVER)
    contacts_ledger.save({"10": 2})
    storage = _checked_storage(monkeypatch, tmp_path, [a, b])
    _contacts([a, b], [{"ID": 10, "Access Hash": 1, "Username": "z"}], tmp_path, storage)

    assert a.added == [] and b.added == [] and contacts_ledger.load() == {"10": 2}  # waits


# --- verify: pick a posts file by button; the scrape's window makes it one click ------------

def _posts(dir_, name, mtime, window=None, groups=("@omni",)):
    path = dir_ / name
    df = pd.DataFrame({"Group": list(groups), "Message ID": range(len(groups))})
    if window:
        df.attrs["scrape_window"] = {"date_min": window[0], "date_max": window[1]}
    df.to_parquet(path)
    os.utime(path, (mtime, mtime))
    return str(path)


def test_posts_bases_lists_only_posts_files(tmp_path, monkeypatch):
    _bases_dir(tmp_path, monkeypatch)  # two _participants bases + one _posts file (mtime 3000)
    newer = _posts(tmp_path, "New_posts_01.01.2026-02.01.2026.parquet", 4_000)
    _posts(tmp_path, "New_posts_01.01.2026-02.01.2026_missed.parquet", 5_000)  # a verify result

    assert [label for _, label in scraped_files.posts_bases()] == [
        "New · 01.01.2026-02.01.2026 · 1", "OmniBase · 01.05.2022-27.09.2026 · 5"]
    assert scraped_files.posts_bases()[0][0] == newer


def _one_account():
    """A pool with one worker: the scraper's account question is skipped."""
    from bot.services.delegation import WorkerPool

    return WorkerPool(_WorkerStorage([_Session(1, "sessions/w.jsession")]))


def test_verify_offers_posts_files(tmp_path, monkeypatch):
    monkeypatch.setattr(scraped_files, "BASES_DIR", str(tmp_path))
    path = _posts(tmp_path, "Omni_posts_01.05.2022-27.09.2026.parquet", 1_000)
    state, callback = _State(), _Callback()

    asyncio.run(scraping_router.verify_start(callback, state, _one_account(), None, None))
    _, markup = callback.message.answers[0]
    assert [b.text for b in _buttons(markup)] == ["Omni · 01.05.2022-27.09.2026 · 1"]
    assert state.data["files"] == [path]


def test_verify_without_posts_files_explains(tmp_path, monkeypatch):
    monkeypatch.setattr(scraped_files, "BASES_DIR", str(tmp_path))
    callback = _Callback()
    asyncio.run(scraping_router.verify_start(callback, _State(), _one_account(), None, None))
    text, markup = callback.message.answers[0]
    assert markup is None and "нет файлов с постами" in text


def _verify_calls(monkeypatch):
    calls = []

    async def do_verify(creds, params):
        calls.append(params)

    monkeypatch.setattr(scraping, "do_verify", do_verify)
    monkeypatch.setattr(scraping_router, "build_credentials", lambda client: None)
    return calls


def test_verify_channel_button_runs_with_the_scrape_window(tmp_path, monkeypatch):
    from bot.services.jobs import JobManager

    monkeypatch.setattr(scraped_files, "BASES_DIR", str(tmp_path))
    path = _posts(tmp_path, "Omni_posts_x.parquet", 1_000, window=("2022-05-01", "2026-09-27"),
                  groups=("@omni", "@c123"))
    calls = _verify_calls(monkeypatch)
    state = _State({"files": [path], "account": "sessions/w.jsession"})

    pick = _Callback()
    asyncio.run(scraping_router.verify_base(pick, ChoiceCB(scope="verify_base", value="0"), state))
    _, markup = pick.message.answers[0]
    assert [b.text for b in _buttons(markup)] == [
        "▶️ @omni · 01.05.2022–27.09.2026", "▶️ -100123 · 01.05.2022–27.09.2026"]

    run = _Callback()
    asyncio.run(scraping_router.verify_channel_pick(
        run, ChoiceCB(scope="verify_channel", value="1"), state, _one_account(), None, JobManager()))
    [params] = calls
    assert (params.input, params.channel) == (path, "-100123")
    assert (params.date_min.date().isoformat(), params.date_max.date().isoformat()) == (
        "2022-05-01", "2026-09-27")
    assert state.data == {}  # flow finished


def test_verify_without_a_window_asks_the_dates(tmp_path, monkeypatch):
    path = _posts(tmp_path, "Old_posts.parquet", 1_000)  # scraped before the window was stored
    calls = _verify_calls(monkeypatch)
    state = _State()

    msg = _Msg()
    asyncio.run(scraping_router._ask_channel(msg, state, path))
    assert [b.text for b in _buttons(msg.answers[0][1])] == ["@omni"]

    pick = _Callback()
    asyncio.run(scraping_router.verify_channel_pick(
        pick, ChoiceCB(scope="verify_channel", value="0"), state, _one_account(), None, None))
    assert pick.message.answers[0][0] == "Дата с:" and state.data["channel"] == "@omni"
    assert calls == []


def test_verify_typed_path_that_can_t_be_read_asks_the_channel(tmp_path):
    msg, state = _Msg(), _State()
    asyncio.run(scraping_router._ask_channel(msg, state, str(tmp_path / "missing.parquet")))
    assert msg.answers == [("Канал (@name / t.me / numeric id):", None)]


# --- analysis: Russian buttons, a file by button, results named and placed automatically ----

def _analysis_run(monkeypatch):
    """A JobManager and the files sent back."""
    from bot.services.jobs import JobManager

    sent = []

    async def send_files(bot, chat_id, paths):
        sent.extend(paths)

    monkeypatch.setattr(scraping_router, "_send_files", send_files)
    return JobManager(), sent


def test_analysis_menu_is_russian():
    callback = _Callback()
    asyncio.run(scraping_router.analysis_start(callback, _State()))
    text, markup = callback.message.answers[0]
    labels = [b.text for b in _buttons(markup)]
    assert labels == ["🔗 Ссылки на другие каналы", "🔎 Поиск постов по словам", "👀 Посмотреть файл", "➕ Ещё"]
    assert "Аккаунты не нужны" in text

    more = _Callback()
    asyncio.run(scraping_router.analysis_tool(more, ChoiceCB(scope="an_tool", value="more"), _State()))
    assert [b.text for b in _buttons(more.message.answers[0][1])] == [
        "🧩 Объединить файлы постов", "💬 Комментарии одной таблицей",
        "📊 Активность по месяцам", "🎲 Случайная выборка постов", "👥 Пересобрать базу участников"]


def test_output_path_next_to_the_input():
    assert scraped_files.output_path("db/Omni_posts_01.05.2022-27.09.2026.parquet", "links") == \
        os.path.join("db", "Omni_links_01.05.2022-27.09.2026")
    assert scraped_files.output_path("db/my file.parquet", "links") == os.path.join("db", "my file_links")


def test_analysis_links_by_button(tmp_path, monkeypatch):
    from scraper import analysis

    monkeypatch.setattr(scraped_files, "BASES_DIR", str(tmp_path))
    path = _posts(tmp_path, "Omni_posts_01.05.2022-27.09.2026.parquet", 1_000)
    seen = []

    def links(input_path, output):
        seen.append((input_path, output))
        out = output + ".xlsx"
        pd.DataFrame({"Telegram Link": ["https://t.me/a", "https://t.me/b"], "Frequency": [5, 2]}).to_excel(
            out, index=False)
        return out

    monkeypatch.setattr(analysis, "links", links)
    manager, sent = _analysis_run(monkeypatch)
    state = _State()

    tool = _Callback()
    asyncio.run(scraping_router.analysis_tool(tool, ChoiceCB(scope="an_tool", value="links"), state))
    text, markup = tool.message.answers[0]
    assert "Ссылки на другие каналы" in text and [b.text for b in _buttons(markup)] == [
        "Omni · 01.05.2022-27.09.2026 · 1"]

    pick = _Callback()
    asyncio.run(scraping_router.analysis_file_pick(pick, ChoiceCB(scope="an_file", value="0"), state, manager))
    assert seen == [(path, str(tmp_path / "Omni_links_01.05.2022-27.09.2026"))]
    replies = [t for t, _ in pick.message.answers]
    assert any("5 — https://t.me/a" in r for r in replies)
    assert sent == [str(tmp_path / "Omni_links_01.05.2022-27.09.2026.xlsx")]
    assert manager.active is False


def test_analysis_filter_asks_for_words(tmp_path, monkeypatch):
    from scraper import analysis

    monkeypatch.setattr(scraped_files, "BASES_DIR", str(tmp_path))
    path = _posts(tmp_path, "Omni_posts.parquet", 1_000)
    seen = []

    def filter_keywords(input_path, output, content_col, keywords, max_rows):
        seen.append((output, keywords))
        open(output + "_unique.xlsx", "w").write("x")

    monkeypatch.setattr(analysis, "filter_keywords", filter_keywords)
    manager, sent = _analysis_run(monkeypatch)
    state = _State({"tool": "filter", "files": [path]})

    pick = _Callback()
    asyncio.run(scraping_router.analysis_file_pick(pick, ChoiceCB(scope="an_file", value="0"), state, manager))
    assert pick.message.answers[0][0].startswith("Слова через запятую")

    msg = _Msg()
    msg.text = "крипта, , биткоин"
    asyncio.run(scraping_router.analysis_keywords(msg, state, manager))
    assert seen == [(str(tmp_path / "Omni_keywords"), ["крипта", "биткоин"])]
    assert sent == [str(tmp_path / "Omni_keywords_unique.xlsx")]


def test_analysis_read_offers_every_file(tmp_path, monkeypatch):
    _bases_dir(tmp_path, monkeypatch)
    state, tool = _State(), _Callback()
    asyncio.run(scraping_router.analysis_tool(tool, ChoiceCB(scope="an_tool", value="read"), state))
    labels = [b.text for b in _buttons(tool.message.answers[0][1])]
    assert labels[0] == "OmniBase_posts_01.05.2022-27.09.2026 · 5 строк" and len(labels) == 3

    manager, _ = _analysis_run(monkeypatch)
    pick = _Callback()
    asyncio.run(scraping_router.analysis_file_pick(pick, ChoiceCB(scope="an_file", value="0"), state, manager))
    assert any("<pre>" in t for t, _ in pick.message.answers)


def test_analysis_combine_all_posts_files(tmp_path, monkeypatch):
    from scraper import analysis

    monkeypatch.setattr(scraped_files, "BASES_DIR", str(tmp_path))
    a = _posts(tmp_path, "A_posts.parquet", 1_000)
    b = _posts(tmp_path, "B_posts.parquet", 2_000)
    _posts(tmp_path, "B_posts_missed.parquet", 3_000)  # a verify result: not merged
    seen = []
    monkeypatch.setattr(analysis, "combine", lambda inputs, output, cols: seen.append((inputs, output)))
    manager, _ = _analysis_run(monkeypatch)
    state, tool = _State(), _Callback()

    asyncio.run(scraping_router.analysis_tool(tool, ChoiceCB(scope="an_tool", value="combine"), state))
    assert [x.text for x in _buttons(tool.message.answers[0][1])] == ["Все файлы постов (2)"]

    asyncio.run(scraping_router.analysis_file_pick(
        _Callback(), ChoiceCB(scope="an_file", value="all"), state, manager))
    assert seen == [([b, a], str(tmp_path / "Combined_posts"))]


def test_analysis_without_posts_files_explains(tmp_path, monkeypatch):
    monkeypatch.setattr(scraped_files, "BASES_DIR", str(tmp_path))
    tool = _Callback()
    asyncio.run(scraping_router.analysis_tool(tool, ChoiceCB(scope="an_tool", value="links"), _State()))
    text, markup = tool.message.answers[0]
    assert markup is None and "нет файлов с постами" in text


# --- mid-run: a worker out for good hands its people over; a passing limit keeps them ---------

def _mailing_with_limit(sessions, rows, limited, monkeypatch, tmp_path, gone, progress=None):
    """Mail with `limited` stopping (PeerFlood) after its first message."""
    from functions import pmmailing
    from functions.base.base import AccountLimited, BaseFunction

    monkeypatch.chdir(tmp_path)
    asked = []

    async def worker_gone(self, session, report):
        asked.append(session)
        return gone

    monkeypatch.setattr(BaseFunction, "worker_gone", worker_gone)
    fn = pmmailing.PmMailingFunc(_WorkerStorage(sessions), ns(
        delay=[0], per_account_daily=100, account_pause=[0]))
    fn.sessions = list(sessions)
    fn.progress = progress

    async def delay():
        pass

    async def deliver(session, peer):
        if session is limited and session.sent:
            raise AccountLimited("PEER_FLOOD")  # what safe_call turns a PeerFloodError into
        session.sent.append(peer)

    fn.delay, fn._deliver = delay, deliver
    msgs = []

    async def report(text):
        msgs.append(text)

    asyncio.run(fn.run(rows, None, [0], report))
    return msgs, asked


_B_ROWS = [{"user_id": i, "access_hash": i, "username": f"u{i}"} for i in (10, 11, 12)]


def test_a_gone_worker_s_people_go_to_the_others(monkeypatch, tmp_path):
    from modules import contacts_ledger

    a, b = _Session(1, "a"), _Session(2, "b")
    contacts_ledger.save({"10": 2, "11": 2, "12": 2})  # all of them b's contacts
    msgs, asked = _mailing_with_limit([a, b], _B_ROWS, b, monkeypatch, tmp_path, gone=True)

    assert asked == [b]
    assert len(b.sent) == 1 and len(a.sent) == 2  # the rest went to a
    assert "Воркер выбыл: 2 его получателей переданы другим воркерам." in msgs


def test_a_worker_on_a_passing_limit_keeps_its_people(monkeypatch, tmp_path):
    from modules import contacts_ledger

    a, b = _Session(1, "a"), _Session(2, "b")
    contacts_ledger.save({"10": 2, "11": 2, "12": 2})
    msgs, asked = _mailing_with_limit([a, b], _B_ROWS, b, monkeypatch, tmp_path, gone=False)

    assert asked == [b] and a.sent == []
    assert "2 получателей ждут своего воркера до следующего запуска." in msgs


def test_a_daily_cap_is_not_asked_about(monkeypatch, tmp_path):
    from modules import contacts_ledger

    a, b = _Session(1, "a"), _Session(2, "b")
    contacts_ledger.save({"10": 2})
    monkeypatch.setattr("modules.account_limits.DailyCounter.reached",
                        lambda self, key: key == "2")
    _, asked = _mailing_with_limit([a, b], _B_ROWS[:1], None, monkeypatch, tmp_path, gone=True)
    assert asked == [] and a.sent == []  # no @SpamBot check, b's contact waits


def test_worker_gone_asks_spambot(monkeypatch, tmp_path):
    import conftest
    from functions.base.base import BaseFunction
    from modules import restricted_workers

    monkeypatch.setattr(BaseFunction, "worker_gone", conftest.REAL_WORKER_GONE)
    forever, dead, clean = (_CheckedSession(1, tmp_path, reply=FOREVER), _CheckedSession(2, tmp_path, dead=True),
                            _CheckedSession(3, tmp_path))
    storage = _checked_storage(monkeypatch, tmp_path, [forever, dead, clean])
    fn = BaseFunction()
    fn.storage, fn.settings = storage, ns(delay=[0])
    msgs = []

    async def report(text):
        msgs.append(text)

    assert asyncio.run(fn.worker_gone(forever, report)) is False  # its people wait for the admin
    assert restricted_workers.load() == [forever.path]
    assert asyncio.run(fn.worker_gone(dead, report)) is True
    assert (tmp_path / "sessions" / "inactive" / "w2.jsession").exists()
    assert asyncio.run(fn.worker_gone(clean, report)) is False
    assert not any(m.startswith("✅") for m in msgs)


# --- 📊 progress: what won't be done this run leaves the total, so the bar can reach 100% ------

def test_mailing_progress_drops_people_waiting_for_their_worker(monkeypatch, tmp_path):
    from bot.services.progress import Progress
    from modules import contacts_ledger

    a, b = _Session(1, "a"), _Session(2, "b")
    contacts_ledger.save({"10": 2, "11": 2, "12": 2})
    progress = Progress()
    _mailing_with_limit([a, b], _B_ROWS, b, monkeypatch, tmp_path, gone=False, progress=progress)
    assert (progress.total, progress.done) == (1, 1)  # 2 wait for b: not this run


def test_mailing_progress_keeps_people_handed_to_others(monkeypatch, tmp_path):
    from bot.services.progress import Progress
    from modules import contacts_ledger

    a, b = _Session(1, "a"), _Session(2, "b")
    contacts_ledger.save({"10": 2, "11": 2, "12": 2})
    progress = Progress()
    _mailing_with_limit([a, b], _B_ROWS, b, monkeypatch, tmp_path, gone=True, progress=progress)
    assert (progress.total, progress.done) == (3, 3)  # b's rest went to a: still sent


class _LimitedSession(_Session):
    """Adds one contact, then is limited."""

    async def __call__(self, request):
        from functions.base.base import AccountLimited

        if self.added:
            raise AccountLimited("PEER_FLOOD")
        await super().__call__(request)


def test_contacts_progress_drops_an_exhausted_queue(tmp_path):
    from bot.services.progress import Progress

    owner, other = _LimitedSession(OWNER, "owner"), _Session(OTHER, "other")
    rows = [{"ID": i, "Access Hash": i, "Username": None, "Owner ID": OWNER} for i in (1, 2)]
    rows += [{"ID": i, "Access Hash": i, "Username": f"u{i}", "Owner ID": OWNER} for i in (3, 4)]
    progress = Progress()
    _contacts([owner, other], rows, tmp_path, progress=progress)

    # the owner's no-username queue stops after 1 of 2; the shared one (other) does both
    assert len(owner.added) == 1 and len(other.added) == 2
    assert (progress.total, progress.done) == (3, 3)


# --- a worker on hold (busy scraping, see WorkerPool.delegate): its people wait for it ----------

def _w_scraping():
    """W (the base's owner, alice in its contacts) is scraping; V is free."""
    from modules import contacts_ledger

    contacts_ledger.save({"1": OWNER})
    w, v = _Session(OWNER, "sessions/w.jsession"), _Session(OTHER, "sessions/v.jsession")
    return w, v, _WorkerStorage([w, v])


def test_adding_contacts_leaves_a_worker_on_hold_its_people(tmp_path):
    from modules import contacts_ledger

    w, v, storage = _w_scraping()
    msgs = _contacts([v], ROWS, tmp_path, storage=storage, on_hold=[w])

    assert v.added == [] and w.added == []  # alice is W's already; the no-username one is W's base
    assert contacts_ledger.load() == {"1": OWNER}
    assert "Уже в контактах (пропущено): 1" in msgs and "Ждут своего воркера (он не участвует в этом запуске): 1" in msgs
    assert not any("база другого аккаунта" in m or "Внимание" in m for m in msgs)


def test_mailing_leaves_a_worker_on_hold_its_people(monkeypatch, tmp_path):
    w, v, storage = _w_scraping()
    msgs, _ = _mailing([v], ROWS, monkeypatch, tmp_path, storage=storage, on_hold=[w])

    assert v.sent == [] and w.sent == []  # nobody writes to W's contact or W's base instead of it
    assert "2 получателей ждут своего воркера: он не участвует в этом запуске." in msgs
    assert not any("база другого аккаунта" in m or "Внимание" in m for m in msgs)


def test_a_released_restricted_worker_on_hold_is_out_for_good(monkeypatch, tmp_path):
    from modules import restricted_workers

    w, v, storage = _w_scraping()
    restricted_workers.save(["sessions/w.jsession"])
    restricted_workers.save_released(["sessions/w.jsession"])
    msgs, _ = _mailing([v], ROWS, monkeypatch, tmp_path, storage=storage, on_hold=[w])

    assert v.sent == ["alice"]  # W's people go to the others; the no-username one only W could reach
    assert "Пропущено бессрочно ограниченных воркеров: 1" in msgs


def test_an_unreleased_restricted_worker_on_hold_keeps_its_people(monkeypatch, tmp_path):
    from modules import restricted_workers

    w, v, storage = _w_scraping()
    restricted_workers.save(["sessions/w.jsession"])
    msgs, _ = _mailing([v], ROWS, monkeypatch, tmp_path, storage=storage, on_hold=[w])

    assert v.sent == []  # alice is W's contact: she waits for the admin's decision
    assert any("ждут своего воркера" in m for m in msgs)


# --- the owner in the shared queue; a worker that ran out ------------------------------------

def test_the_owner_alone_takes_the_shared_queue_too(monkeypatch, tmp_path):
    owner = _Session(OWNER, "o.jsession")  # known by its .jsession: the base's owner
    _mailing([owner], ROWS, monkeypatch, tmp_path)
    assert len(owner.sent) == 2  # alice (username, shared) too, not only the one without


def test_a_worker_limited_on_its_own_queue_sends_no_more(monkeypatch, tmp_path):
    from functions.base.base import AccountLimited
    from modules import contacts_ledger

    class _Flooded(list):  # a's sends hit a flood restriction
        calls = 0

        def append(self, item):
            type(self).calls += 1
            raise AccountLimited("flood")

    a, b = _Session(1, "a"), _Session(2, "b")
    a.sent = _Flooded()
    contacts_ledger.save({"10": 1})
    rows = [{"user_id": 10, "access_hash": 1, "username": "x"},
            {"user_id": 20, "access_hash": 2, "username": "y"}]
    _mailing([a, b], rows, monkeypatch, tmp_path)

    assert _Flooded.calls == 1  # its own contact only: the shared one goes to b, not to a again
    assert b.sent


def test_contacts_owner_limited_on_its_own_queue_is_not_tried_again(tmp_path):
    from functions.base.base import AccountLimited

    class _Limited(_Session):
        calls = 0

        async def __call__(self, request):
            type(self).calls += 1
            raise AccountLimited("flood")

    owner = _Limited(OWNER, "o.jsession")
    _contacts([owner], [{"ID": 1, "Access Hash": 11, "Username": None, "Owner ID": OWNER},
                        {"ID": 2, "Access Hash": 22, "Username": "bob", "Owner ID": OWNER}], tmp_path)
    assert _Limited.calls == 1  # its own queue stopped it: the shared one is not tried on it


def test_contacts_path_upload_does_not_launch_the_default_base():
    """A file sent instead of a typed path must re-prompt, not run on assets/contacts.parquet."""
    class _Manager:
        async def run(self, *args, **kwargs):
            raise AssertionError("must not launch")

    message = _Msg()
    message.text, message.document = None, ns(file_name="base.parquet")
    state = _State({"bases": []})
    asyncio.run(audience.run(message, state, ns(), {"AddContactsFunc": object()}, _Manager(), ns(delay=[1])))
    assert message.answers and state.data == {"bases": []}  # re-prompted, flow kept


# --- CLI: a worker left out of "how many accounts to use?" keeps its contacts ----------

def _picked_first_of(sessions, cls, settings, monkeypatch):
    from functions.base.base import BaseFunction

    monkeypatch.setattr(BaseFunction, "ask_int", staticmethod(lambda *a, **k: 1))
    fn = cls(_WorkerStorage(sessions), settings)
    fn.ask_accounts_count()
    return fn


def test_cli_subset_mailing_leaves_unpicked_workers_contacts(monkeypatch, tmp_path):
    from functions.pmmailing import PmMailingFunc
    from modules import contacts_ledger

    monkeypatch.chdir(tmp_path)
    a, b = _Session(1, "a"), _Session(2, "b")
    contacts_ledger.save({"1": 2})  # alice is in b's contacts
    fn = _picked_first_of([a, b], PmMailingFunc,
                          ns(delay=[0], per_account_daily=100, account_pause=[0]), monkeypatch)

    async def deliver(session, peer):
        session.sent.append(peer)

    fn._deliver = deliver
    msgs = []

    async def report(text):
        msgs.append(text)

    asyncio.run(fn.run([ROWS[0]], None, [0], report))
    assert a.sent == [] and b.sent == []  # a stranger to her: she waits for b


def test_cli_subset_contacts_skip_unpicked_workers_people(monkeypatch, tmp_path):
    from functions.add_contacts import AddContactsFunc
    from modules import contacts_ledger

    monkeypatch.chdir(tmp_path)
    path = tmp_path / "base.parquet"
    pd.DataFrame({"ID": [1], "Access Hash": [11], "Username": ["alice"]}).to_parquet(path)
    a, b = _Session(1, "a"), _Session(2, "b")
    contacts_ledger.save({"1": 2})
    fn = _picked_first_of([a, b], AddContactsFunc,
                          ns(delay=[0], contacts_per_account_daily=0), monkeypatch)

    async def report(text):
        pass

    asyncio.run(fn.run(str(path), [0], report))
    assert a.added == [] and contacts_ledger.load() == {"1": 2}
