"""The warmup (modules/warmup.py, functions/warmup.py): its settings, who is warmed when, each step's requests."""
import asyncio
import io
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace as ns

import pytest
from PIL import Image
from telethon import types
from telethon.errors import FreshResetAuthorisationForbiddenError

from functions.changeusername import first_free
from functions.warmup import WarmupFunc
from modules import content, restricted_workers, warmup

import conftest


# --- the module ---

def _assets(region="ru", **pools):
    folder = content.path(region, "")
    os.makedirs(folder, exist_ok=True)
    for name, lines in pools.items():
        with open(os.path.join(folder, f"{name}.txt"), "w", encoding="utf-8") as fileobj:
            fileobj.write("\n".join(lines))
    return folder


def _photo(folder, name="a.jpg"):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    Image.new("RGB", (200, 160), (200, 100, 50)).save(path, "JPEG")
    return path


def test_settings_start_from_the_defaults_and_each_button_cycles_its_value():
    settings = warmup.load_settings(conftest.home())
    assert settings["region"] == "ru" and settings["days"] == 14 and settings["steps"]["username"] == warmup.KEEP
    # a field that may be empty: off → on → only an empty one → off; the others (a name never is): on ↔ off
    assert [warmup.cycle(settings, "bio")["steps"]["bio"] for _ in range(3)] == [warmup.KEEP, warmup.OFF, warmup.ON]
    assert [warmup.cycle(settings, "name")["steps"]["name"] for _ in range(2)] == [warmup.OFF, warmup.ON]
    assert [warmup.cycle(settings, "2fa")["steps"]["2fa"] for _ in range(2)] == [warmup.OFF, warmup.ON]
    settings["steps"]["name"] = warmup.KEEP  # kept by an older version: taken as on
    warmup.save_settings(conftest.home(), settings)
    assert warmup.load_settings(conftest.home())["steps"]["name"] == warmup.ON
    assert warmup.region(conftest.home()) == "ru"


def test_a_pool_skips_blank_lines_and_comments_and_a_missing_one_is_empty():
    _assets(bots=["# bots", "", "@gif", "  @vid  "])
    assert content.pool("ru", "bots") == ["@gif", "@vid"] and content.pool("en", "bots") == []


def test_usernames_are_made_of_the_name_and_are_valid():
    names = warmup.username_candidates("Ольга", "Петрова")
    assert names and all(name.startswith(("olga", "petrova", "o")) for name in names)
    assert "olgapetrova" in names and "olga_petrova" in names
    assert all(5 <= len(name) <= 32 and "__" not in name and not name.endswith("_") for name in names)
    assert all(name[0].isalpha() for name in warmup.username_candidates("Li", None))


def test_a_unique_photo_is_another_jpeg_each_time(tmp_path):
    path = _photo(str(tmp_path))
    first, second = warmup.unique_photo(path), warmup.unique_photo(path)
    assert first.name == "photo.jpg" and Image.open(first).format == "JPEG"
    assert first.getvalue() != open(path, "rb").read()
    assert Image.open(io.BytesIO(second.getvalue())).size[0] <= 200


class _Storage:
    def __init__(self, paths):
        self.directory = os.path.join(conftest.home(), "sessions")
        os.makedirs(self.directory, exist_ok=True)
        self.home = conftest.home()
        self.jsessions_paths = {}
        self.full_sessions = {}
        for path in paths:
            open(os.path.join(self.directory, path), "w").close()
            self.full_sessions[os.path.join(self.directory, path)] = _Client(path)
        self.remembered = {}

    @property
    def sessions(self):
        return list(self.full_sessions.values())

    def get_session_path(self, session):
        return next((path for path, client in self.full_sessions.items() if client is session), None)

    @asynccontextmanager
    async def ainitialize_session(self, session):
        yield

    def remember_name(self, session, first, last):
        self.remembered["name"] = (first, last)

    def remember_username(self, session, username):
        self.remembered["username"] = username

    def remember_password(self, session, password):
        self.remembered["password"] = password


def test_due_leaves_out_the_warmed_the_ones_warmed_today_and_the_restricted():
    storage = _Storage(["a.session", "b.session", "c.session", "d.session", "e.session"])
    a, b, c, d, e = sorted(storage.full_sessions)
    warmup.update(a, status=warmup.WARMED, last="2000-01-01")
    warmup.update(b, status=warmup.WARMING, last=warmup.today())
    warmup.update(c, status=warmup.FAILED, last="2000-01-01")
    restricted_workers.save([d])
    assert warmup.due(storage, [a, b, c, d, e]) == [c, e]


def test_categories_count_every_account_file_of_the_project():
    storage = _Storage(["a.session", "b.session", "c.session", "d.session", "e.session"])
    a, b, c, d, e = sorted(storage.full_sessions)
    warmup.update(a, status=warmup.WARMED, warmed_at=warmup.today())
    warmup.update(b, status=warmup.WARMING)
    warmup.update(c, status=warmup.FAILED)
    restricted_workers.save([d])
    os.makedirs(os.path.join(storage.directory, "inactive"))
    open(os.path.join(storage.directory, "inactive", "z.session"), "w").close()
    found = warmup.categories(storage)
    assert {name: len(paths) for name, paths in found.items()} == {
        warmup.NEW: 1, warmup.WARMING: 1, warmup.WARMED: 1, warmup.FAILED: 1, warmup.RESTRICTED: 1, warmup.DEAD: 1}
    assert found[warmup.NEW] == [e] and warmup.warmed_counts(storage) == (1, 1)
    # 🔥 Прогрев's and the card's line: the live ones warmed, today's, the restricted and the dead
    assert warmup.summary(storage) == "✅ Прогрето 1 из 5 (сегодня 1) · ⛔ 1 · 💀 1"


def test_summary_is_empty_without_accounts():
    assert warmup.summary(_Storage([])) == ""


# --- the function ---

class _Client:
    """A worker's Telethon client: answers each request by its type, records them."""

    def __init__(self, name, username=None, about="", photos=(), has_password=False):
        self.name, self.requests, self.sent, self.pinned = name, [], [], []
        self.me = ns(id=1000, first_name="Old", last_name=None, username=username)  # an id of 2013: an old account
        self.full = ns(about=about, profile_photo=object() if photos else None, saved_music=None)
        self.photos = list(photos)
        self.has_password = has_password
        self.terminate_error = None
        self.active = 0  # requests in flight right now: the threads' check
        self.most = 0

    async def get_me(self):
        return self.me

    async def __call__(self, request):
        self.active += 1
        self.most = max(self.most, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        kind = type(request).__name__
        self.requests.append(kind)
        if kind == "GetFullUserRequest":
            return ns(full_user=self.full)
        if kind == "GetPasswordRequest":
            return ns(has_password=self.has_password)
        if kind == "ResetAuthorizationsRequest" and self.terminate_error:
            raise self.terminate_error
        if kind == "JoinChannelRequest":  # as Telegram answers it (layer 229): the Updates wrapped
            return types.messages.ChatInviteJoinResultOk(updates=ns(chats=[ns(id=hash(request.channel))]))
        return True

    async def get_profile_photos(self, peer):
        return self.photos

    async def upload_file(self, file):
        return file

    async def send_message(self, peer, text):
        self.sent.append((peer, text))
        return ns(id=len(self.sent))

    async def pin_message(self, peer, message, notify=False):
        self.pinned.append(message)

    async def get_messages(self, channel, limit):
        return [ns(id=3), ns(id=2)]

    async def send_read_acknowledge(self, peer, max_id=None):
        self.requests.append("read")

    async def edit_2fa(self, new_password):
        self.requests.append("edit_2fa")

    def iter_dialogs(self, limit):
        async def none():
            return
            yield
        return none()


@pytest.fixture(autouse=True)
def _no_bot_wait(monkeypatch):
    from functions import warmup as warmup_func

    monkeypatch.setattr(warmup_func, "BOT_ANSWER", (0, 0))


def _func(storage):
    return WarmupFunc(storage, ns(delay=[0], profile_pause=[0]))


def _content(photos=1):
    folder = _assets(names=["Иван Петров", "Анна Смирнова"], bios=["Живу моментом"], channels=["@one", "@two"],
                     bots=["@gif"], stickers=["HotCherry"], notes=["купить хлеб", "позвонить"])
    for index in range(photos):
        _photo(os.path.join(folder, "photos"), f"{index}.jpg")


def _settings(**changes):
    settings = warmup.load_settings(conftest.home())
    for key, value in changes.items():
        settings[key] = value
    warmup.save_settings(conftest.home(), settings)
    return settings


def _run(func):
    reports = []

    async def report(text):
        reports.append(text)

    asyncio.run(func.run(report))
    return reports


def test_the_first_day_sets_up_the_profile_and_security_and_the_last_one_warms(monkeypatch):
    _content()
    _settings(days=1, music_chance=0, stickers_chance=100, bots_chance=100, channels_count="2",
              steps={**warmup.DEFAULTS["steps"], "username": warmup.ON})
    storage = _Storage(["a.jsession"])
    _on_proxies(storage, [None])  # a .jsession: its 2FA password has a place to be kept
    client = storage.sessions[0]
    reports = _run(_func(storage))

    for request in ("UpdateProfileRequest", "UpdateUsernameRequest", "UploadProfilePhotoRequest",
                    "InstallStickerSetRequest", "SetPrivacyRequest", "ResetAuthorizationsRequest",
                    "JoinChannelRequest", "GetMessagesViewsRequest"):
        assert request in client.requests, request
    assert "edit_2fa" in client.requests and storage.remembered["password"]
    assert storage.remembered["name"] in (("Иван", "Петров"), ("Анна", "Смирнова"))
    assert storage.remembered["username"].startswith(("ivan", "petrov", "anna", "smirnova", "i", "a"))
    assert ("@gif", "/start") in client.sent and client.pinned  # a bot started, a note pinned
    assert any("вступил в каналов: 2" in line for line in reports) and not any("⚠️" in line for line in reports)
    state = warmup.load_state()["1"]  # its user id
    assert state["status"] == warmup.WARMED and state["day"] == 1 and state["warmed_at"] == warmup.today()
    assert set(warmup.SETUP) <= set(state["done"]) and any("✅ Прогреты" in line for line in reports)


def test_a_session_worker_gets_no_2fa_password_it_could_not_keep():
    _content()
    _settings(days=1, music_chance=0)
    storage = _Storage(["a.session"])  # a StringSession: no .jsession to keep a password in
    client = storage.sessions[0]
    reports = _run(_func(storage))
    assert "edit_2fa" not in client.requests and "GetPasswordRequest" not in client.requests
    assert "password" not in storage.remembered and any("негде сохранить пароль" in line for line in reports)
    state = warmup.load_state()[next(iter(storage.full_sessions))]
    assert "2fa" in state["done"] and state["status"] == warmup.WARMED  # skipped: the warmup goes on


def test_kept_fields_stay_and_a_fresh_session_s_reset_is_left_for_the_next_day():
    _content()
    _settings(days=2, music_chance=0, steps={**warmup.DEFAULTS["steps"], "bio": warmup.KEEP})
    storage = _Storage(["a.session"])
    client = storage.sessions[0]
    client.me.username, client.full.about = "taken_name", "my bio"
    client.terminate_error = FreshResetAuthorisationForbiddenError(None)
    _run(_func(storage))

    path = next(iter(storage.full_sessions))
    state = warmup.load_state()[path]
    # the first day: the security only, its reset refused a fresh session
    assert "terminate" not in state["done"] and state["status"] == warmup.FAILED and state["day"] == 1
    assert "UpdateProfileRequest" not in client.requests
    # the next (last) day: the reset again, and the profile — the filled fields kept
    warmup.update(path, last="2000-01-01")
    client.requests.clear()
    client.terminate_error = None
    _run(_func(storage))
    state = warmup.load_state()[path]
    assert "ResetAuthorizationsRequest" in client.requests and "terminate" in state["done"] and state["day"] == 2
    assert "UpdateUsernameRequest" not in client.requests and "username" in state["done"]  # kept: done
    assert "bio" in state["done"] and "username" not in storage.remembered
    assert "UploadProfilePhotoRequest" in client.requests  # the rest of the profile set up


def test_an_account_s_first_day_of_several_is_its_security_only():
    _content()
    _settings(days=3)  # its security today, its profile over the 2 days after
    storage = _Storage(["a.jsession"])
    _on_proxies(storage, [None])  # a .jsession: its 2FA password has a place to be kept
    client = storage.sessions[0]
    reports = _run(_func(storage))
    for request in ("SetPrivacyRequest", "edit_2fa", "ResetAuthorizationsRequest"):
        assert request in client.requests, request
    assert "UpdateProfileRequest" not in client.requests and "UploadProfilePhotoRequest" not in client.requests
    assert client.pinned  # the day's activity as every day: a note in Saved Messages, pinned
    state = warmup.load_state()["1"]
    assert state["done"] == ["lastseen", "2fa", "terminate"] and state["status"] == warmup.WARMING
    assert any("день 1 из 3" in line for line in reports)


def test_an_account_is_warmed_once_a_day():
    _content()
    storage = _Storage(["a.session"])
    _run(_func(storage))
    assert _run(_func(storage)) == ["Некого прогревать сегодня: все прогреты, уже прогревались сегодня или ограничены."]


def _on_proxies(storage, proxies):
    """Each account (by name order) on its proxy of `proxies` (an "ip" each; None: without one)."""
    from modules.types.proxy import Proxy

    for user_id, (path, ip) in enumerate(zip(sorted(storage.full_sessions), proxies), 1):
        account = ns(user_id=user_id, phone_number=f"7999000000{user_id}", first_name="Old", last_name=None,
                     username=None)
        storage.jsessions_paths[path] = ns(account=ns(proxy=Proxy("socks5", ip, 1080) if ip else None,
                                                      account=account, password=None))


def _most_at_once(monkeypatch, storage) -> tuple[int, dict]:
    """Run the warmup: (the most accounts warmed at once, the most at once per proxy)."""
    running, most, per_proxy, most_per_proxy = 0, 0, {}, {}
    real = WarmupFunc.warm

    async def warm(self, session, run, report):
        nonlocal running, most
        key = warmup.proxy_key(storage, storage.get_session_path(session))
        running, per_proxy[key] = running + 1, per_proxy.get(key, 0) + 1
        most, most_per_proxy[key] = max(most, running), max(most_per_proxy.get(key, 0), per_proxy[key])
        await asyncio.sleep(0.01)
        running, per_proxy[key] = running - 1, per_proxy[key] - 1
        await real(self, session, run, report)

    monkeypatch.setattr(WarmupFunc, "warm", warm)
    _run(_func(storage))
    return most, most_per_proxy


@pytest.mark.parametrize("threads", [1, 3])
def test_no_more_accounts_than_its_threads_are_warmed_at_once(monkeypatch, threads):
    _content(photos=0)
    _settings(threads=threads, music_chance=0)
    storage = _Storage([f"{n}.session" for n in range(5)])
    _on_proxies(storage, ["1.1.1.1", "2.2.2.2", "3.3.3.3", "4.4.4.4", "5.5.5.5"])
    assert _most_at_once(monkeypatch, storage)[0] == threads and len(warmup.load_state()) == 5


def test_the_accounts_of_one_proxy_are_warmed_one_at_a_time(monkeypatch):
    """However many threads: never two accounts of one address side by side (those without a proxy: the
    server's)."""
    _content(photos=0)
    _settings(threads=150, music_chance=0)
    storage = _Storage([f"{n}.session" for n in range(7)])
    _on_proxies(storage, ["1.1.1.1", "1.1.1.1", "1.1.1.1", "2.2.2.2", "2.2.2.2", None, None])
    assert warmup.proxy_count(storage) == 3
    most, per_proxy = _most_at_once(monkeypatch, storage)
    assert most == 3 and per_proxy == {"1.1.1.1:1080": 1, "2.2.2.2:1080": 1, "": 1}
    assert len(warmup.load_state()) == 7


def test_the_new_avatars_go_up_before_the_old_ones_are_removed():
    from telethon.tl.types import Photo

    _content()
    _settings(music_chance=0, steps={**{key: warmup.OFF for key in warmup.STEPS}, "photo": warmup.ON})
    storage = _Storage(["a.session"])
    client = storage.sessions[0]
    client.photos = [Photo(id=1, access_hash=2, file_reference=b"", date=None, sizes=[], dc_id=1)]
    _run(_func(storage))
    assert client.requests.index("UploadProfilePhotoRequest") < client.requests.index("DeletePhotosRequest")


def test_a_day_where_no_step_went_through_is_not_counted():
    _content()
    _settings(days=1)  # 🛡 username by default: the profile is read first
    storage = _Storage(["a.session"])
    client = storage.sessions[0]

    async def refuse(request):
        raise ConnectionError("нет связи")

    client.__class__ = type("_Down", (_Client,), {"__call__": lambda self, request: refuse(request)})
    reports = _run(_func(storage))
    state = warmup.load_state()[next(iter(storage.full_sessions))]
    assert state["status"] == warmup.FAILED and state.get("day", 0) == 0 and state["last"] == warmup.today()
    assert any("день не засчитан" in line for line in reports)


def test_broken_warmup_files_read_as_empty():
    os.makedirs(os.path.dirname(warmup.STATE_PATH), exist_ok=True)
    for path in (warmup.STATE_PATH, os.path.join(conftest.home(), warmup.SETTINGS_NAME)):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as fileobj:
            fileobj.write("{broken")
    assert warmup.load_state() == {} and warmup.load_settings(conftest.home())["days"] == warmup.DEFAULTS["days"]
    assert warmup.summary(_Storage(["a.session"])).startswith("✅ Прогрето 0 из 1")


def test_two_accounts_looking_at_once_never_pick_the_same_free_username():
    """The warmup's threads share `used`: a candidate is taken before Telegram answers, not after."""
    class _Session:
        async def __call__(self, request):
            await asyncio.sleep(0)  # the other account asks meanwhile
            return True

    async def both():
        used = set()
        return await asyncio.gather(first_free(_Session(), ["olga", "olga_p"], used),
                                    first_free(_Session(), ["olga", "olga_p"], used))

    assert sorted(asyncio.run(both())) == ["olga", "olga_p"]
