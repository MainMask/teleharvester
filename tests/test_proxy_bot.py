"""Offline tests for the bot proxy-setup handler (bot/routers/accounts/proxies.py)."""

import asyncio
import conftest
import types

from bot.routers import accounts
from modules.types.proxy import Proxy


def ns(**kw):
    return types.SimpleNamespace(**kw)


class _State:
    def __init__(self):
        self.cleared, self.state = False, None

    async def clear(self):
        self.cleared = True

    async def set_state(self, state):
        self.state = state


class _Msg:
    def __init__(self, text=None, document=None):
        self.text = text
        self.document = document
        self.replies = []

    async def answer(self, text, **kwargs):
        self.replies.append(text)

    async def edit_text(self, text, **kwargs):  # in place: what is shown now, as an answer
        self.replies.append(text)


def _manager(active=False, label=""):
    """The project's job slot: busy (`active`, with its job's `label`) or free."""
    return ns(active=active, label=label, project_id=1)


class _Pool:
    def __init__(self, storage, polling=None, scraping=()):
        self.storage = storage
        self.polling = polling  # the worker autoreply is polling now (WorkerPool.polling)
        self.scraping = scraping  # the session paths a scrape runs on

    def busy(self, path):
        return False

    def scraped(self, path):
        return path in self.scraping


class _Storage:
    home = property(lambda self: conftest.home())  # the project's folder: its proxies.txt

    def __init__(self):
        self.applied = None
        self.jsessions_paths = {}

    def apply_proxies(self, proxies, spare=(), restricted=()):
        self.applied = proxies
        return {"accounts": 6, "proxies_used": 2, "restricted": 0, "string_sessions_skipped": 0}


def test_text_proxies_applied_and_file_written(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "assets").mkdir()

    storage = _Storage()
    msg = _Msg(text="socks5://u:p@1.1.1.1:1080\nhttp://2.2.2.2:3128")
    state = _State()

    asyncio.run(accounts.proxies.proxy_apply(msg, state, _Pool(storage), _manager()))

    assert len(storage.applied) == 2
    assert state.state == accounts.proxies.SetProxy.spare  # next: the optional proxies for the restricted
    assert (tmp_path / "home" / "proxies.txt").read_text().strip().splitlines() == [
        "socks5://u:p@1.1.1.1:1080",
        "http://2.2.2.2:3128",
    ]
    assert any("применены" in r for r in msg.replies)


def test_refuses_while_job_active(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "assets").mkdir()

    storage = _Storage()
    msg = _Msg(text="socks5://u:p@1.1.1.1:1080")
    state = _State()

    asyncio.run(accounts.proxies.proxy_apply(msg, state, _Pool(storage), _manager(True, "Рассылка")))

    assert storage.applied is None  # nothing applied
    assert state.cleared is False
    assert not (tmp_path / "home" / "proxies.txt").exists()


def test_bad_proxy_line_reports(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "assets").mkdir()

    storage = _Storage()
    msg = _Msg(text="garbage-line")
    state = _State()

    asyncio.run(accounts.proxies.proxy_apply(msg, state, _Pool(storage), _manager()))

    assert storage.applied is None
    assert state.cleared is False
    assert any("разобрать" in r for r in msg.replies)


def test_refuses_while_autoreply_polls(tmp_path, monkeypatch):
    # the polled worker's old client stays connected through its old proxy until the poll ends
    monkeypatch.chdir(tmp_path)
    (tmp_path / "assets").mkdir()

    storage = _Storage()
    msg = _Msg(text="socks5://u:p@1.1.1.1:1080")
    state = _State()

    pool = _Pool(storage, polling="sessions/a.jsession")
    asyncio.run(accounts.proxies.proxy_apply(msg, state, pool, _manager()))

    assert storage.applied is None
    assert state.cleared is False
    assert any("автоответа" in r for r in msg.replies)


# --- the proxies for the permanently restricted workers -------------------------------------------

class _Rehoming(_Storage):
    def __init__(self, on=()):
        super().__init__()
        self.rehomed = []
        # the workers' proxies, by URL (the retired list keeps the ones still in use)
        self.jsessions_paths = {url: ns(account=ns(proxy=Proxy.from_url(url))) for url in on}

    def rehome_restricted(self, restricted, main, spare, dry_run=False, busy=()):
        self.rehomed.append((len(main), len(spare), dry_run, sorted(busy)))
        moving = "busy" if "a" in busy else "out"  # its one move: "a"
        return {"out": [], "back": [], "stuck": [], "busy": [], moving: ["a"]}


def _write(tmp_path, name, text):
    (tmp_path / "home").mkdir(exist_ok=True)
    (tmp_path / "home" / name).write_text(text)


def test_the_restricted_proxies_are_saved_and_the_restricted_moved(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "proxies_restricted.txt", "socks5://8.8.8.8:1080\n")  # replaced: its workers found too
    storage, state = _Rehoming(on=["socks5://8.8.8.8:1080"]), _State()
    msg = _Msg(text="socks5://9.9.9.9:1080")

    asyncio.run(accounts.proxies.proxy_spare_apply(msg, state, _Pool(storage), _manager()))

    assert (tmp_path / "home" / "proxies_restricted.txt").read_text() == "socks5://9.9.9.9:1080\n"
    assert storage.rehomed == [(0, 1, False, [])] and state.cleared
    assert any("На прокси для ограниченных: 1" in r for r in msg.replies)


def test_the_restricted_proxies_may_not_repeat_the_pool(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "proxies.txt", "socks5://9.9.9.9:1080\n")
    storage, state = _Rehoming(), _State()
    msg = _Msg(text="socks5://9.9.9.9:1080")

    asyncio.run(accounts.proxies.proxy_spare_apply(msg, state, _Pool(storage), _manager()))

    assert storage.rehomed == [] and not state.cleared
    assert not (tmp_path / "home" / "proxies_restricted.txt").exists()


def test_a_worker_scraping_is_left_for_later_the_others_moved(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    storage, state = _Rehoming(), _State()
    storage.jsessions_paths["a"] = ns(account=ns(proxy=None))  # its one move, scraping now
    msg = _Msg(text="socks5://9.9.9.9:1080")

    asyncio.run(accounts.proxies.proxy_spare_apply(msg, state, _Pool(storage, scraping=["a"]),
                                                   _manager()))

    assert storage.rehomed == [(0, 1, False, ["a"])]  # moved the rest, "a" left as it is
    assert (tmp_path / "home" / "proxies_restricted.txt").exists() and state.cleared
    assert any("Заняты сейчас (сбор или вход): 1" in r for r in msg.replies)


def test_not_separating_clears_the_list_and_brings_them_back(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "proxies_restricted.txt", "socks5://9.9.9.9:1080\n")
    storage, state = _Rehoming(on=["socks5://9.9.9.9:1080"]), _State()  # one still on it: stuck, say
    cb = ns(message=_Msg(), answer=lambda *a, **k: asyncio.sleep(0))

    asyncio.run(accounts.proxies.proxy_spare_choice(cb, ns(value="clear"), state, _Pool(storage),
                                                    _manager()))

    assert not (tmp_path / "home" / "proxies_restricted.txt").exists() and state.cleared
    assert storage.rehomed[-1] == (0, 0, False, [])  # no list for them now: everyone off the pool comes back
    assert any("больше не отделяются" in r for r in cb.message.replies)


def test_a_broken_list_for_the_restricted_comes_first(tmp_path, monkeypatch):
    """Its step before the pool's: counted in the pool, a pool sized for the others would fall short."""
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "proxies_restricted.txt", "garbage\n")
    storage, state = _Storage(), _State()
    msg = _Msg(text="socks5://u:p@1.1.1.1:1080")

    asyncio.run(accounts.proxies.proxy_apply(msg, state, _Pool(storage), _manager()))

    assert storage.applied is None and state.state == accounts.proxies.SetProxy.spare
    assert any("испорчен" in r and "Сначала" in r for r in msg.replies)


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((text, kwargs.get("reply_markup")))


def _workspace(storage, project_id=7, active=False):
    return ns(project_id=project_id, pool=_Pool(storage), manager=ns(active=active, label="Рассылка"))


def test_the_status_check_offers_the_move_only_when_there_is_one(tmp_path, monkeypatch):
    from bot.routers import spamblock

    bot = _Bot()
    monkeypatch.setattr(spamblock, "rehome", lambda storage, dry_run=False: storage)
    asyncio.run(spamblock.offer_rehome(bot, 1, _workspace({"out": [], "back": [], "stuck": ["x"]})))
    assert bot.sent == []  # nothing a press would change
    asyncio.run(spamblock.offer_rehome(bot, 1, _workspace({"out": ["a", "b"], "back": [], "stuck": []})))
    (text, markup), = bot.sent
    assert "на их прокси: 2" in text
    assert markup.inline_keyboard[0][0].callback_data == "ch:rehome:7"  # the project it was offered for


def test_a_broken_list_is_told_after_the_check(monkeypatch):
    from bot.routers import spamblock

    def broken(storage, dry_run=False):
        raise ValueError("ошибка в proxies_restricted.txt: неверный прокси")

    bot = _Bot()
    monkeypatch.setattr(spamblock, "rehome", broken)
    asyncio.run(spamblock.offer_rehome(bot, 1, _workspace(None)))
    assert "невозможна" in bot.sent[0][0]


class _Callback:
    def __init__(self, age_hours=0):
        import datetime

        self.message = _Msg()
        self.message.date = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=age_hours)
        self.alert = None

    async def answer(self, text=None, show_alert=False, **kwargs):
        if show_alert:
            self.alert = text


def test_the_move_button_moves_its_own_project_and_waits_for_a_job(monkeypatch):
    from bot.routers import spamblock

    moved = []
    monkeypatch.setattr("bot.routers.accounts.proxies.rehome", lambda storage, dry_run=False, busy=():
                        moved.append(storage.name) or {"out": [], "back": [], "stuck": [], "busy": []})
    mine = _workspace(ns(name="mine", jsessions_paths={}), 7, active=True)
    other = _workspace(ns(name="other", jsessions_paths={}), 8)
    workspaces = ns(get=lambda pid: {7: mine, 8: other}.get(pid))

    cb = _Callback()
    asyncio.run(spamblock.rehome_restricted(cb, ns(value="7"), workspaces))
    assert moved == [] and any("Занято: идёт «Рассылка»" in r for r in cb.message.replies)  # its job runs

    mine.manager.active = False
    edits = []
    cb = _Callback()
    cb.message.edit_text = lambda text, **k: asyncio.sleep(0, edits.append(text))
    asyncio.run(spamblock.rehome_restricted(cb, ns(value="7"), workspaces))
    assert moved == ["mine"] and edits  # project 7's, whatever the chat is in


def test_a_stale_move_button_moves_nothing(monkeypatch):
    from bot.routers import spamblock

    moved = []
    monkeypatch.setattr(spamblock, "rehome", lambda storage, dry_run=False: moved.append(storage))
    cb = _Callback(age_hours=25)
    asyncio.run(spamblock.rehome_restricted(cb, ns(value="7"), ns(get=lambda pid: _workspace("mine"))))
    assert moved == [] and "устарела" in cb.alert


def test_a_button_from_before_the_project_id_is_stale(monkeypatch):
    from bot.routers import spamblock

    cb = _Callback()
    asyncio.run(spamblock.rehome_restricted(cb, ns(value="go"), ns(get=lambda pid: _workspace("mine"))))
    assert "устарела" in cb.alert


def test_a_bad_pool_list_writes_nothing_for_the_restricted(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "proxies.txt", "garbage\n")
    storage, state = _Rehoming(), _State()
    msg = _Msg(text="socks5://9.9.9.9:1080")

    asyncio.run(accounts.proxies.proxy_spare_apply(msg, state, _Pool(storage), _manager()))

    assert not (tmp_path / "home" / "proxies_restricted.txt").exists() and storage.rehomed == []


def test_a_broken_list_for_the_restricted_can_be_replaced_from_the_bot(tmp_path, monkeypatch):
    """No dead end: the list being replaced is not read before it is written."""
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "proxies_restricted.txt", "garbage\n")
    storage, state = _Rehoming(), _State()
    msg = _Msg(text="socks5://9.9.9.9:1080")

    asyncio.run(accounts.proxies.proxy_spare_apply(msg, state, _Pool(storage), _manager()))

    assert (tmp_path / "home" / "proxies_restricted.txt").read_text() == "socks5://9.9.9.9:1080\n"
    assert storage.rehomed[-1] == (0, 1, False, []) and state.cleared


def test_a_broken_list_for_the_restricted_can_be_dropped_too(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, "proxies_restricted.txt", "garbage\n")
    storage, state = _Storage(), _State()
    msg = _Msg(text="socks5://u:p@1.1.1.1:1080")

    sent = []
    async def answer(text, **kwargs):
        sent.append(kwargs.get("reply_markup"))
    msg.answer = answer

    asyncio.run(accounts.proxies.proxy_apply(msg, state, _Pool(storage), _manager()))

    labels = [b.text for markup in sent if markup for row in markup.inline_keyboard for b in row]
    assert "🗑 Не отделять" in labels
