"""The profile / security ledger (modules/profile_done.py), the functions that mark it, the
avatar replacement, the picks without repeats and the CLI's worker picker."""

import asyncio
import contextlib
import datetime
import types
from unittest.mock import patch

from telethon.tl.functions.photos import DeletePhotosRequest, UploadProfilePhotoRequest
from telethon.tl.types import Photo

from functions.base.telethon import TelethonFunction
from modules import json_file, profile_done, restricted_workers


def ns(**kw):
    return types.SimpleNamespace(**kw)


def _jsession(user_id, first_name=None, last_name=None):
    return ns(account=ns(account=ns(user_id=user_id, first_name=first_name, last_name=last_name,
                                    username=None, phone_number="+1")))


def _storage(sessions, paths=None, names=None):
    """Workers as .jsessions: the one at path "w" is user id "w" (named names["w"], if given)."""
    @contextlib.asynccontextmanager
    async def ainitialize_session(session):
        yield

    paths, names = paths or {}, names or {}
    return ns(sessions=list(sessions), ainitialize_session=ainitialize_session,
              jsessions_paths={path: _jsession(path, *names.get(path, ())) for path in paths.values()},
              get_session_path=lambda session: paths.get(id(session)),
              remember_name=lambda session, first, last: None)


async def _quiet(text):
    pass


def _done():
    return profile_done.load()["done"]


# --- the ledger ------------------------------------------------------------------------------

def test_seed_marks_the_current_workers_once():
    profile_done.seed(["1", None, "2"])  # None: a StringSession, not tracked
    profile_done.seed(["3"])
    assert set(_done()) == {"1", "2"} and set(_done()["1"]) == set(profile_done.TRACKED)


def test_mark_dates_the_change_and_keeps_the_value():
    profile_done.mark("1", "ChangeBioFunc", "hi")
    profile_done.mark("2", "ClearPersonalChannelFunc")
    profile_done.mark(None, "ChangeBioFunc")
    assert _done() == {"1": {"ChangeBioFunc": datetime.date.today().isoformat()},
                       "2": {"ClearPersonalChannelFunc": datetime.date.today().isoformat()}}
    assert profile_done.used("ChangeBioFunc") == ["hi"]


def test_the_worker_is_its_user_id_not_its_file():
    storage = ns(jsessions_paths={"sessions/a.jsession": _jsession(42), "sessions/b.jsession": _jsession(42)})
    assert profile_done.worker_key(storage, "sessions/a.jsession") == "42"
    assert profile_done.worker_key(storage, "sessions/b.jsession") == "42"  # re-imported under another name
    assert profile_done.worker_key(storage, "sessions/x.session") is None
    assert profile_done.worker_key(storage, None) is None


def test_notes():
    ledger = {"done": {"1": {"ChangeBioFunc": "2026-10-07"}, "2": {"ChangeBioFunc": profile_done.SEEDED}}}
    status = {"b": "01.11.2026"}
    assert profile_done.notes("1", "a", "ChangeBioFunc", ledger, {"a"}, status) == ["✓ 07.10", "⛔"]
    assert profile_done.notes("2", "b", "ChangeBioFunc", ledger, set(), status) == ["✓", "🚫 до 01.11.2026"]
    assert profile_done.notes("1", "a", "ChangeNameFunc", ledger, set(), {"a": "active"}) == []
    assert profile_done.notes(None, "a", "ChangeBioFunc", ledger, set(), {}) == []


def test_pick_fresh_takes_the_least_used():
    assert profile_done.pick_fresh(["a", "b", "c"], ["a", "b"]) == "c"
    assert profile_done.pick_fresh(["a", "b"], ["a", "b", "a"]) == "b"
    assert profile_done.pick_fresh(["a"], ["a", "a"]) == "a"


# --- the functions mark it on success only ---------------------------------------------------

class _Session:
    def __init__(self, error=None):
        self.error, self.requests = error, []

    async def get_me(self):
        return ns(first_name="Acc", last_name=None)

    async def __call__(self, request):
        if self.error:
            raise self.error
        self.requests.append(request)


def test_bio_marks_the_changed_workers_only():
    from functions.changebio import ChangeBioFunc

    ok, failing = _Session(), _Session(error=RuntimeError("boom"))
    fn = ChangeBioFunc(_storage([ok, failing], {id(ok): "ok", id(failing): "failing"}), ns(profile_pause=[0]))
    asyncio.run(fn.run(_quiet, bio="hi"))
    assert set(_done()) == {"ok"}


def test_request_each_tells_success():
    ok, failing = _Session(), _Session(error=RuntimeError("boom"))
    fn = TelethonFunction(_storage([]), ns())
    assert asyncio.run(fn.request_each(ok, _quiet, object(), "done", "failed")) is True
    assert asyncio.run(fn.request_each(failing, _quiet, object(), "done", "failed")) is False


def test_request_each_counts_for_the_job_summary():
    from bot.services.progress import Progress

    ok, failing = _Session(), _Session(error=RuntimeError("boom"))
    fn = TelethonFunction(_storage([]), ns())
    fn.progress = Progress()
    for session in (ok, ok, failing):
        asyncio.run(fn.request_each(session, _quiet, object(), "done", "failed"))
    assert (fn.progress.ok, fn.progress.failed) == (2, 1)


# --- no repeats: a list's / the folder's values go to the workers that do not have them -------

def test_bios_from_a_list_skip_the_ones_other_workers_have():
    from functions.changebio import ChangeBioFunc

    json_file.save(profile_done.PATH, {"done": {}, "values": {"ChangeBioFunc": {"old": "one"}}})
    b, c = _Session(), _Session()
    fn = ChangeBioFunc(_storage([b, c], {id(b): "b", id(c): "c"}), ns(profile_pause=[0]))
    asyncio.run(fn.run(_quiet, bios=["one", "two", "three"]))
    assert {b.requests[0].about, c.requests[0].about} == {"two", "three"}
    assert sorted(profile_done.used("ChangeBioFunc")) == ["one", "three", "two"]


def test_names_from_a_list_skip_the_ones_workers_have():
    from functions.changename import ChangeNameFunc

    old, new = _Session(), _Session()
    storage = _storage([new], {id(old): "old", id(new): "new"}, names={"old": ("Иван", "Петров")})
    fn = ChangeNameFunc(storage, ns(profile_pause=[0]))
    asyncio.run(fn.run(_quiet, names=["Иван  Петров", "Анна"]))
    assert (new.requests[0].first_name, new.requests[0].last_name) == ("Анна", "")


def test_photos_from_the_folder_skip_the_ones_other_workers_have(tmp_path, monkeypatch):
    from functions.change_profile_photo import ChangeProfilePhotoFunc

    monkeypatch.chdir(tmp_path)
    (tmp_path / "assets" / "photos").mkdir(parents=True)
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        (tmp_path / "assets" / "photos" / name).write_bytes(b"x")
    json_file.save(profile_done.PATH, {"done": {}, "values": {"ChangeProfilePhotoFunc": {"old": "a.jpg"}}})

    set_to = []

    async def fake_set(session, photo_path, report):
        set_to.append(photo_path.rsplit("/", 1)[-1])

    fn = ChangeProfilePhotoFunc(_storage([object(), object()]), ns(profile_pause=[0]))
    fn.set_profile_photo = fake_set
    asyncio.run(fn.run(_quiet))
    assert sorted(set_to) == ["b.jpg", "c.jpg"]


# --- the avatar replaces the old ones -----------------------------------------------------------

class _PhotoSession(_Session):
    def __init__(self, photos, delete_error=None):
        super().__init__()
        self.photos, self.delete_error = photos, delete_error

    async def upload_file(self, path):
        return "file"

    async def get_profile_photos(self, entity):
        return [Photo(id=i, access_hash=0, file_reference=b"", date=None, sizes=[], dc_id=1) for i in self.photos]

    async def __call__(self, request):
        self.requests.append(request)
        if isinstance(request, UploadProfilePhotoRequest):
            self.photos.insert(0, 3)
            return ns(photo=ns(id=3))
        if self.delete_error:
            raise self.delete_error


def _set_photo(session, progress=None):
    from functions.change_profile_photo import ChangeProfilePhotoFunc

    fn = ChangeProfilePhotoFunc(_storage([session], {id(session): "w"}), ns(profile_pause=[0]))
    fn.progress = progress
    reports = []

    async def report(text):
        reports.append(text)

    asyncio.run(fn.run(report, photo_path="/x/avatar.jpg"))
    return reports


def test_new_avatar_first_then_the_old_ones_deleted():
    session = _PhotoSession([1, 2])
    reports = _set_photo(session)

    upload, delete = session.requests
    assert isinstance(upload, UploadProfilePhotoRequest) and isinstance(delete, DeletePhotosRequest)
    assert [photo.id for photo in delete.id] == [1, 2]
    assert reports == ["[Acc] фото загружено (/x/avatar.jpg), старых удалено: 2"]
    assert "ChangeProfilePhotoFunc" in _done()["w"]
    assert profile_done.used("ChangeProfilePhotoFunc") == ["avatar.jpg"]


def test_failed_deletion_is_reported_and_still_marked():
    from bot.services.progress import Progress

    progress = Progress()
    reports = _set_photo(_PhotoSession([1], delete_error=RuntimeError("flood")), progress)
    assert reports == ["[Acc] фото загружено (/x/avatar.jpg), старые не удалены: flood"]
    assert "ChangeProfilePhotoFunc" in _done()["w"]
    assert (progress.ok, progress.failed) == (1, 0)  # the avatar is set: a success, not an error


def test_no_old_avatar_nothing_deleted():
    session = _PhotoSession([])
    assert _set_photo(session) == ["[Acc] фото загружено (/x/avatar.jpg), старых удалено: 0"]
    assert len(session.requests) == 1


# --- the CLI picker ---------------------------------------------------------------------------

def test_parse_picks():
    parse = TelethonFunction.parse_picks
    assert parse("", 5, [3, 4]) == [3, 4] and parse("new", 5, [3, 4]) == [3, 4]
    assert parse("all", 3, []) == [0, 1, 2]
    assert parse("1, 3,4-5", 5, []) == [0, 2, 3, 4]
    assert parse("6", 5, []) == [] and parse("0", 5, []) == [] and parse("x", 5, []) == []
    assert parse("3-2", 5, []) == []


def test_cli_picks_the_new_workers_by_default():
    from functions.changebio import ChangeBioFunc

    old, new = object(), object()
    storage = _storage([old, new], {id(old): "old", id(new): "new"})
    profile_done.seed(["old"])
    restricted_workers.save(["old"])
    fn = ChangeBioFunc(storage, ns())

    with patch("functions.base.telethon.Prompt.ask", side_effect=[""]):
        fn.ask_workers()
    assert fn.sessions == [new] and fn.on_hold == []

    profile_done.mark("new", "ChangeBioFunc")
    with patch("functions.base.telethon.Prompt.ask", side_effect=["", "1"]):  # no new ones: asked again
        fn.ask_workers()
    assert fn.sessions == [old]
