"""🆕 Комментарии — к новым постам: the workers watch channels and comment each new post one by one
(functions/broadcast_watch.py), and its steps in the bot (bot/routers/chat_broadcasts.py)."""
import asyncio
import contextlib
import os
import types
from datetime import datetime, timedelta, timezone

import pytest
from telethon.tl.types import MessageActionPinMessage, MessageService, PeerChannel

from bot.callbacks import FunctionCB
from bot.routers import chat_broadcasts as broadcasts
from bot.states import ChatPost
from functions import broadcast_comments, broadcast_watch
from functions.base.base import AccountLimited
from functions.broadcast_comments import CommentsBroadcastFunc
from functions.broadcast_watch import CommentsWatchFunc
from modules import projects, rich_message, watch_state
from modules.rich_message import MediaItem, RichContent

import conftest
from test_broadcast_options import _Callback, _Manager, _Msg, _Settings, _State, _content


def ns(**kw):
    return types.SimpleNamespace(**kw)


def _post(post_id, grouped_id=None, comments=True, age=timedelta(0)):
    return ns(id=post_id, grouped_id=grouped_id, replies=ns(comments=True) if comments else None,
              date=datetime.now(timezone.utc) - age)


class _Session:
    """A worker reading the channels' `feed` (channel -> its posts): newest first, as Telethon gives them."""

    def __init__(self, name, feed, unreadable=()):
        self.name, self.feed, self.unreadable = name, feed, set(unreadable)
        self.session = ns(_entities=set())  # Telethon's: what the updates of its chats fill
        self.looked_up = []  # get_input_entity's peers: an invite is a request each time

    async def get_input_entity(self, peer):
        self.looked_up.append(peer)
        return peer

    async def get_messages(self, peer, limit=None, min_id=0):
        if peer in self.unreadable or peer not in self.feed:
            raise ValueError(f"{peer}: нет доступа")
        return sorted((post for post in self.feed[peer] if post.id > min_id), key=lambda post: -post.id)[:limit]


def _watch(monkeypatch, sessions, sent, fail=lambda session: None, cap=0, tmp_path=None):
    """The function on these workers, each comment recorded in `sent` as (worker, channel, post) — or raised by
    `fail(session)` —, its content in `function.texts`; `cap`: the daily comments a worker (its counter under
    `tmp_path`); no waits: the polls and the pauses take a turn of the event loop only."""
    if tmp_path is not None:
        monkeypatch.setattr(broadcast_comments, "COMMENTS_LIMITS", str(tmp_path / "comments_limits.json"))
    texts = []
    monkeypatch.setattr(broadcast_watch, "POLL_SECONDS", 0)

    async def connect(session):
        pass
    monkeypatch.setattr(broadcast_watch, "connect_client", connect)

    async def send(session, peer, content, safe_call, report=None, comment_to=None):
        if (error := fail(session)) is not None:
            raise error
        sent.append((session.name, peer, comment_to))
        texts.append(content)
    monkeypatch.setattr(rich_message, "send", send)

    released = []

    @contextlib.asynccontextmanager
    async def ainitialize_session(session):
        try:
            yield
        finally:
            released.append(session.name)

    storage = ns(sessions=list(sessions), ainitialize_session=ainitialize_session, jsessions_paths={},
                 get_session_path=lambda session: f"/w/{session.name}.jsession")
    function = CommentsWatchFunc(storage, ns(delay=[0], comments_per_account_daily=cap))
    function.released, function.texts = released, texts
    return function


async def _until(condition):
    for _ in range(2000):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("not reached")


def _scenario(function, channels, per_post, steps, **run):
    """Run the watch (`run`: more of its arguments); `steps`: (condition, then) — wait for the condition, then call
    then(); ⏹ at the end. Returns the reports."""
    reports = []

    async def report(text):
        reports.append(text)

    async def main():
        task = asyncio.create_task(function.run(channels, per_post, [0], [object()], report, **run))
        for condition, then in steps:
            await _until(lambda: condition(reports) or task.done())
            then()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    asyncio.run(main())
    return reports


def test_only_posts_after_the_start_get_comments_each_by_another_worker(monkeypatch):
    feed, sent = {"@ch": [_post(10)]}, []
    workers = [_Session(name, feed) for name in ("a", "b", "c")]
    function = _watch(monkeypatch, workers, sent)
    _scenario(function, ["@ch"], 2, [
        (lambda reports: any(r.startswith("Слежу за каналами: 1") for r in reports),
         lambda: feed["@ch"].append(_post(11))),
        (lambda reports: len(sent) == 2, lambda: None)])
    assert [post for _, _, post in sent] == [11, 11]  # 10 was there before the start
    assert len({worker for worker, _, _ in sent}) == 2  # never twice by one worker
    assert sorted(function.released) == ["a", "b", "c"]  # ⏹: every worker let go


def test_the_first_comment_of_each_post_is_by_the_next_worker_whatever_the_reads(monkeypatch):
    """The reads between two posts (a channel a round, however many rounds) must not decide who comments first."""
    function = _watch(monkeypatch, ["x", "y"], [])
    function._workers, function._out, function._turns = ["x", "y"], set(), {"read": 0, "post": 0}
    leaders = []
    for reads in (1, 3, 1, 2):  # one channel, a post found every round, or after a few
        for _ in range(reads):
            function._order("read")
        leaders.append(function._order("post")[0])
    assert leaders == ["x", "y", "x", "y"]


def test_every_worker_comments_with_no_limit_and_never_more_than_once(monkeypatch):
    feed, sent = {"@ch": []}, []
    function = _watch(monkeypatch, [_Session(name, feed) for name in ("a", "b")], sent)
    _scenario(function, ["@ch"], 0, [
        (lambda reports: any(r.startswith("Слежу") for r in reports), lambda: feed["@ch"].append(_post(1))),
        (lambda reports: len(sent) == 2, lambda: None)])
    assert sorted(worker for worker, _, _ in sent) == ["a", "b"]


def test_an_album_is_commented_once_and_a_post_without_comments_is_skipped(monkeypatch):
    feed, sent = {"@ch": []}, []
    function = _watch(monkeypatch, [_Session("a", feed)], sent)

    def publish():
        feed["@ch"] += [_post(2, grouped_id=7, comments=False), _post(3, grouped_id=7), _post(4, comments=False)]

    reports = _scenario(function, ["@ch"], 1, [
        (lambda reports: any(r.startswith("Слежу") for r in reports), publish),
        (lambda reports: sent and any("пост 4" in r for r in reports), lambda: None)])
    assert sent == [("a", "@ch", 3)]  # the album's post that has the comments
    assert "@ch · пост 4: комментарии закрыты — пропущен" in reports


def test_a_service_message_is_no_post(monkeypatch):
    """A pin or a new photo in the channel (Telethon's MessageService, no replies) is neither commented nor said to
    have its comments closed: the scraper leaves them out too."""
    feed, sent = {"@ch": []}, []
    function = _watch(monkeypatch, [_Session("a", feed)], sent)
    pin = MessageService(id=2, peer_id=PeerChannel(1), date=datetime.now(timezone.utc),
                         action=MessageActionPinMessage())
    reports = _scenario(function, ["@ch"], 1, [
        (lambda reports: any(r.startswith("Слежу") for r in reports), lambda: feed["@ch"].extend([pin, _post(3)])),
        (lambda reports: sent, lambda: None)])
    assert sent == [("a", "@ch", 3)]
    assert not any("пост 2" in r for r in reports)


def test_an_album_split_by_a_round_is_commented_once_and_only_the_last_read_s_albums_are_kept(monkeypatch):
    """A watch runs for weeks: the albums it remembers (one split by a round must not be commented twice) are each
    channel's last read's, not every album it ever saw."""
    feed, sent, seen = {"@ch": []}, [], []
    function = _watch(monkeypatch, [_Session("a", feed)], sent)
    _scenario(function, ["@ch"], 1, [
        (lambda reports: any(r.startswith("Слежу") for r in reports),
         lambda: feed["@ch"].append(_post(2, grouped_id=7))),
        (lambda reports: sent, lambda: feed["@ch"].append(_post(3, grouped_id=7))),  # the album's rest, a round later
        (lambda reports: seen[-1] == {"@ch": 3}, lambda: feed["@ch"].append(_post(4, grouped_id=8))),
        (lambda reports: len(sent) == 2, lambda: None)], on_seen=seen.append)
    assert [post for _, _, post in sent] == [2, 4]
    assert function._albums == {"@ch": {8}}


def test_a_capped_worker_is_said_once_a_day_and_only_its_last_day_is_kept(monkeypatch):
    function = _watch(monkeypatch, [], [])
    function._limits = ns(cap=1, reached=lambda key: True)
    function._capped = {}
    today, reports = [datetime(2026, 1, 1).date()], []

    async def report(text):
        reports.append(text)

    async def main():
        monkeypatch.setattr(broadcast_watch, "date", ns(today=lambda: today[0]))
        session = _Session("a", {})
        for day in (1, 1, 2, 3, 3):
            today[0] = datetime(2026, 1, day).date()
            assert await function._capped_today(session, report)
    asyncio.run(main())
    assert len(reports) == 3  # once a day
    assert len(function._capped) == 1  # a watch runs for weeks: not a day kept for each


def test_a_worker_out_by_a_limit_comments_no_more_and_with_none_left_the_watch_ends(monkeypatch):
    feed, sent = {"@ch": []}, []
    function = _watch(monkeypatch, [_Session("a", feed)], sent, fail=lambda session: AccountLimited("FloodWait"))
    reports = _scenario(function, ["@ch"], 1, [
        (lambda reports: any(r.startswith("Слежу") for r in reports), lambda: feed["@ch"].append(_post(1))),
        (lambda reports: any("выбыли" in r for r in reports), lambda: None)])
    assert sent == [] and any(r.startswith("[a.jsession] лимит") for r in reports)
    assert reports[-1] == "Все воркеры выбыли по лимитам — слежка остановлена."
    assert not any("не читается" in r for r in reports)


def test_a_channel_no_worker_reads_is_left_out_and_with_none_the_watch_ends(monkeypatch):
    feed, sent = {"@ok": []}, []
    function = _watch(monkeypatch, [_Session("a", feed)], sent)
    reports = _scenario(function, ["@ok", "@closed"], 1,
                        [(lambda reports: any(r.startswith("Слежу") for r in reports), lambda: None)])
    assert "[!] @closed не читается: @closed: нет доступа" in reports
    assert any(r.startswith("Слежу за каналами: 1.") for r in reports)

    function = _watch(monkeypatch, [_Session("a", feed)], sent)
    reports = _scenario(function, ["@closed"], 1, [(lambda reports: False, lambda: None)])  # until it ends
    assert reports[-1] == "Ни один канал не читается — слежка остановлена."


def test_a_channel_one_worker_can_t_read_is_read_by_the_next(monkeypatch):
    feed, sent = {"@ch": []}, []
    function = _watch(monkeypatch, [_Session("a", feed, unreadable={"@ch"}), _Session("b", feed)], sent)
    reports = _scenario(function, ["@ch"], 1, [
        (lambda reports: any(r.startswith("Слежу") for r in reports), lambda: feed["@ch"].append(_post(1))),
        (lambda reports: sent, lambda: None)])
    assert not any("не читается" in r for r in reports)


def test_a_channel_typed_as_an_id_or_a_link_is_read_by_what_telethon_resolves(monkeypatch):
    """CHAT_FORMAT's forms: an id as its peer (a string id Telethon takes for a phone), a t.me/s/ or a post's link as
    @name (Telethon parses neither); the typed form stays the reports' and the state's."""
    feed, sent = {1234567890: [], "@name": []}, []

    class Worker(_Session):
        async def get_messages(self, peer, **kwargs):  # the channel's posts by its bare id (TL objects: no hash)
            return await super().get_messages(peer.channel_id if isinstance(peer, PeerChannel) else peer, **kwargs)

    function = _watch(monkeypatch, [Worker("a", feed)], sent)
    seen = []

    def publish():
        feed[1234567890].append(_post(5))
        feed["@name"].append(_post(6))

    reports = _scenario(function, ["-1001234567890", "t.me/s/name/3"], 1, [
        (lambda reports: any(r.startswith("Слежу за каналами: 2") for r in reports), publish),
        (lambda reports: len(sent) == 2, lambda: None)], on_seen=seen.append)
    assert sorted(post for _, _, post in sent) == [5, 6]
    assert not any("не читается" in r for r in reports)
    assert seen[-1] == {"-1001234567890": 5, "t.me/s/name/3": 6}


def test_a_channel_is_looked_up_once_a_worker_not_every_round(monkeypatch):
    """An invite is checked by Telethon anew on every use (CheckChatInvite): once a minute it would end in a flood
    wait."""
    invite = "https://t.me/+AbCdEf"
    feed, sent = {invite: []}, []
    worker = _Session("a", feed)
    function = _watch(monkeypatch, [worker], sent)
    _scenario(function, [invite], 1, [
        (lambda reports: any(r.startswith("Слежу") for r in reports), lambda: feed[invite].append(_post(1))),
        (lambda reports: sent, lambda: feed[invite].append(_post(2))),
        (lambda reports: len(sent) == 2, lambda: None)])
    assert worker.looked_up == [invite]  # its start, its reads and its comments: one lookup


def test_the_entities_its_chats_updates_bring_are_dropped_past_the_listener_s_limit(monkeypatch):
    """Connected for the whole watch, a worker gets the updates of all its chats: Telethon keeps every user and chat
    they name, as for 🔔's listener."""
    monkeypatch.setattr(broadcast_watch, "LISTENER_ENTITY_LIMIT", 2)
    feed, sent = {"@ch": []}, []
    worker = _Session("a", feed)
    function = _watch(monkeypatch, [worker], sent)
    _scenario(function, ["@ch"], 1, [
        (lambda reports: any(r.startswith("Слежу") for r in reports),
         lambda: worker.session._entities.update({1, 2, 3})),
        (lambda reports: not worker.session._entities, lambda: None)])
    assert worker.session._entities == set()


def test_a_daily_cap_set_while_the_watch_runs_applies_from_the_next_round(monkeypatch, tmp_path):
    feed, sent = {"@ch": []}, []
    function = _watch(monkeypatch, [_Session("a", feed)], sent, cap=0, tmp_path=tmp_path)
    def cap_and_publish():  # set in ⚙️ after the start: 0 (none) when it began
        function.settings.comments_per_account_daily = 1
        feed["@ch"].append(_post(1))

    reports = _scenario(function, ["@ch"], 1, [
        (lambda reports: any(r.startswith("Слежу") for r in reports), cap_and_publish),
        (lambda reports: sent, lambda: feed["@ch"].append(_post(2))),
        (lambda reports: any("ни одного комментария" in r for r in reports), lambda: None)])
    assert sent == [("a", "@ch", 1)]  # its one of today: none under 2
    assert "[a.jsession] дневной лимит комментариев — до завтра" in reports


def test_stop_cancels_the_comments_still_waiting_and_lets_the_workers_go(monkeypatch):
    feed, sent = {"@ch": []}, []
    function = _watch(monkeypatch, [_Session("a", feed)], sent)
    waiting = []

    async def send(session, peer, content, safe_call, report=None, comment_to=None):
        waiting.append(comment_to)
        await asyncio.Event().wait()  # never set: the comment is still being written at ⏹
    monkeypatch.setattr(rich_message, "send", send)

    _scenario(function, ["@ch"], 1, [
        (lambda reports: any(r.startswith("Слежу") for r in reports), lambda: feed["@ch"].append(_post(1))),
        (lambda reports: waiting, lambda: None)])
    assert waiting == [1] and sent == [] and function.released == ["a"]


# --- the bot's steps ---

def _workspaces(channels):
    workspaces = conftest.Workspaces()
    workspaces.open(1, projects.add("Green", channels))
    return workspaces


def test_the_project_s_channels_are_one_tap_away():
    callback, state = _Callback(), _State()
    asyncio.run(broadcasts.start(callback, FunctionCB(key="watch"), state, ns(count=lambda: 2),
                                 _workspaces(["@a", "@b"])))
    text, markup = callback.message.answers[-1]
    assert text.startswith("🆕 Комментарии — к новым постам — за какими каналами следить?")
    assert state.state == ChatPost.channels
    assert markup.inline_keyboard[0][0].text == "📢 Каналы проекта (2)"

    asyncio.run(broadcasts.watch_own(_Callback(), state, ns(count=lambda: 2)))
    assert state.data["channels"] == ["@a", "@b"] and state.state == ChatPost.per_post


def test_a_project_without_channels_asks_for_them_typed():
    callback, state = _Callback(), _State()
    asyncio.run(broadcasts.start(callback, FunctionCB(key="watch"), state, ns(count=lambda: 2), _workspaces([])))
    text, markup = callback.message.answers[-1]
    assert [button.text for row in markup.inline_keyboard for button in row] == ["✖️ Отмена"]

    asyncio.run(broadcasts.watch_channels(_Msg("@x, t.me/y"), state, ns(count=lambda: 2)))
    assert state.data["channels"] == ["@x", "t.me/y"] and state.state == ChatPost.per_post


@pytest.mark.parametrize("raw", ["0", "два", ""])
def test_a_wrong_comments_count_is_asked_again(raw):
    state, msg = _State({"flow": "watch"}), _Msg(raw)
    asyncio.run(broadcasts.per_post_input(msg, state))
    assert "per_post" not in state.data and "Нужно число от 1" in msg.answers[-1][0]


def test_the_pause_is_typed_in_minutes_and_kept_in_seconds():
    state = _State({"flow": "watch"})
    asyncio.run(broadcasts.per_post_all(_Callback(), state))
    assert state.data["per_post"] == 0 and state.state == ChatPost.gap

    msg = _Msg("пять")
    asyncio.run(broadcasts.gap_input(msg, state))
    assert "gap" not in state.data and "Нужно число минут" in msg.answers[-1][0]

    asyncio.run(broadcasts.gap_input(_Msg("3-10"), state))
    assert state.data["gap"] == [180, 600] and state.state == ChatPost.message


def _pool(workers=1):
    return ns(storage=ns(home=conftest.home()), count=lambda: workers, workers=[object()] * workers)


def test_the_message_starts_the_watch_kept_on_disk_and_saves_no_settings(monkeypatch):
    content = _content(monkeypatch)
    content.text, content.entities, content.media = "привет", [], []
    settings, manager = _Settings(), _Manager()
    state = _State({"flow": "watch", "channels": ["@a"], "per_post": 2, "gap": [60, 300]})
    asyncio.run(broadcasts.message_input(_Msg(), state, None, _pool(), {}, manager, settings))
    [args] = manager.runs
    factory, header = args[5], args[6]
    assert header.startswith("Слежу за каналами (1): комментарии под новыми постами по 2, пауза 1–5 мин.")
    assert settings.saved == [] and state.data == {}
    kept = watch_state.load(conftest.home())
    assert kept["channels"] == ["@a"] and kept["per_post"] == 2 and kept["gap"] == [60, 300] and kept["chat_id"] == 1

    calls = []
    factory(ns(run=lambda *a, **k: calls.append((a, k))), "report")
    [(positional, named)] = calls
    assert positional == (["@a"], 2, [60, 300], [content], "report") and named["last"] is None
    named["on_seen"]({"@a": 7})  # the newest post seen goes to disk: a restart continues after it
    assert watch_state.load(conftest.home())["last"] == {"@a": 7}


def test_a_txt_on_the_message_step_is_the_comments_variants(monkeypatch):
    manager = _Manager()
    _content(monkeypatch)  # its resolve: no function looked up

    async def read(message, max_size):
        return message.document.text
    monkeypatch.setattr(broadcasts, "read_text_document", read)

    state = _State({"flow": "comments", "link": "https://t.me/c/1/2", "messages_count": 1})
    empty = _Msg(document=ns(file_name="v.TXT", text=" \n\n"))
    asyncio.run(broadcasts.message_input(empty, state, None, _pool(), {}, manager, _Settings()))
    assert manager.runs == [] and "нет ни одного варианта" in empty.answers[-1][0] and state.data  # the step stays

    asyncio.run(broadcasts.message_input(_Msg(document=ns(file_name="v.txt", text="первый\n\n второй \n")), state,
                                         None, _pool(), {}, manager, _Settings()))
    [args] = manager.runs
    calls = []
    args[5](ns(run=lambda *a: calls.append(a)), "report")
    [(link, contents, _)] = calls
    assert [content.text for content in contents] == ["первый", "второй"]


def test_the_message_step_offers_the_variants_to_the_comments_only():
    for flow, offered in (("comments", True), ("watch", True), ("instant", False)):
        msg, state = _Msg(), _State({"flow": flow})
        asyncio.run(broadcasts._ask_message(msg.answer, state))
        assert ("Или пришлите .txt со списком вариантов" in msg.answers[-1][0]) == offered, flow


# --- after a restart ---

def test_resume_continues_a_kept_watch_after_the_posts_it_saw(monkeypatch):
    watch_state.save(conftest.home(), 5, ["@a"], 1, [60], [RichContent(text="hi")])
    watch_state.update_last(conftest.home(), {"@a": 7})
    manager, sent = _Manager(), []

    async def send_message(chat_id, text, **kwargs):
        sent.append((chat_id, text))
    workspace = ns(pool=_pool(), functions={}, manager=manager)
    monkeypatch.setattr(broadcasts, "resolve", lambda functions, key: (object(), object()))
    asyncio.run(broadcasts.resume_watches(ns(send_message=send_message), ns(all=lambda: [workspace])))
    assert sent == [(5, "🔄 Бот перезапущен — продолжаю «🆕 Комментарии — к новым постам».")]
    [args] = manager.runs
    calls = []
    args[5](ns(run=lambda *a, **k: calls.append((a, k))), "report")
    [(positional, named)] = calls
    assert positional[:3] == (["@a"], 1, [60]) and positional[3][0].text == "hi" and named["last"] == {"@a": 7}


def test_resume_without_workers_says_so_and_forgets_the_watch():
    watch_state.save(conftest.home(), 5, ["@a"], 1, [60], [RichContent(text="hi")])
    manager, sent = _Manager(), []

    async def send_message(chat_id, text, **kwargs):
        sent.append(text)
    asyncio.run(broadcasts.resume_watches(ns(send_message=send_message),
                                          ns(all=lambda: [ns(pool=_pool(0), functions={}, manager=manager)])))
    assert manager.runs == [] and "нет воркеров" in sent[0] and watch_state.load(conftest.home()) is None


def test_the_end_of_a_watch_forgets_it_but_a_bot_stopping_keeps_it(monkeypatch):
    monkeypatch.setattr(broadcasts, "resolve", lambda functions, key: (object(), object()))
    cleanups = []

    class _Keeping(_Manager):
        async def run(self, *args, **kwargs):
            cleanups.append(kwargs["cleanup"])
            return True

    watch = {"channels": ["@a"], "per_post": 1, "gap": [60], "contents": [RichContent(text="hi")]}
    asyncio.run(broadcasts._start_watch(None, 5, _pool(), {}, _Keeping(), dict(watch)))
    monkeypatch.setattr(watch_state, "shutting_down", True)
    cleanups[-1]()  # the bot stops: cancelled, kept
    assert watch_state.load(conftest.home()) is not None
    monkeypatch.setattr(watch_state, "shutting_down", False)
    cleanups[-1]()  # ⏹, its end, or a start refused
    assert watch_state.load(conftest.home()) is None


def test_the_kept_message_comes_back_as_it_was_its_media_in_the_project_s_folder(tmp_path):
    from telethon import types as tl

    photo = tmp_path / "bcast" / "0_photo.jpg"
    photo.parent.mkdir()
    photo.write_bytes(b"jpg")
    content = RichContent(text="😀 жирный", entities=[tl.MessageEntityCustomEmoji(0, 2, document_id=5),
                                                     tl.MessageEntityBold(3, 6)],
                          media=[MediaItem(str(photo), animated=True)], temp_paths=[str(photo)],
                          temp_dir=str(photo.parent))
    watch_state.save(conftest.home(), 5, ["@a"], 0, [60, 120], [content])
    assert not photo.parent.exists()  # moved: tmp/broadcast is swept at every start

    [back] = watch_state.load(conftest.home())["contents"]
    assert back.text == "😀 жирный" and back.entities == content.entities
    [media] = back.media
    assert media.animated and media.path.startswith(conftest.home()) and open(media.path, "rb").read() == b"jpg"

    watch_state.drop(conftest.home())
    assert watch_state.load(conftest.home()) is None and not os.path.exists(media.path)


# --- the function: after a restart, the variants, the daily cap ---

def test_a_restart_comments_the_fresh_posts_it_missed_not_the_old_ones(monkeypatch):
    feed, sent, seen = {"@ch": [_post(5), _post(6, age=timedelta(hours=5)), _post(7, age=timedelta(minutes=30))]}, \
        [], []
    function = _watch(monkeypatch, [_Session("a", feed)], sent)
    reports = _scenario(function, ["@ch"], 1, [(lambda reports: sent, lambda: None)],
                        last={"@ch": 5}, on_seen=seen.append)
    assert sent == [("a", "@ch", 7)] and "@ch: пропущено старых постов (старше 3 ч): 1" in reports
    assert seen[-1] == {"@ch": 7}


def test_the_variants_under_a_post_are_all_different(monkeypatch):
    feed, sent = {"@ch": []}, []
    function = _watch(monkeypatch, [_Session(name, feed) for name in "abc"], sent)
    variants = [RichContent(text=text) for text in ("один", "два", "три")]
    reports = []

    async def report(text):
        reports.append(text)

    async def main():
        task = asyncio.create_task(function.run(["@ch"], 0, [0], variants, report))
        await _until(lambda: any(r.startswith("Слежу") for r in reports))
        feed["@ch"].append(_post(1))
        await _until(lambda: len(sent) == 3)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    asyncio.run(main())
    assert sorted(content.text for content in function.texts) == ["два", "один", "три"]


def test_a_worker_at_its_daily_cap_is_skipped_and_the_cap_is_the_comments_one(monkeypatch, tmp_path):
    feed, sent = {"@ch": []}, []
    function = _watch(monkeypatch, [_Session("a", feed), _Session("b", feed)], sent, cap=1, tmp_path=tmp_path)
    # a has written its comment today in 🗨 Комментарии — к посту: the same counter
    broadcast_comments.comments_cap(ns(comments_per_account_daily=1)).bump("/w/a.jsession")
    reports = _scenario(function, ["@ch"], 2, [
        (lambda reports: any(r.startswith("Слежу") for r in reports), lambda: feed["@ch"].append(_post(1))),
        (lambda reports: sent, lambda: feed["@ch"].append(_post(2))),
        (lambda reports: any("ни одного комментария" in r for r in reports), lambda: None)])
    assert sent == [("b", "@ch", 1)]  # under 2 nobody: b wrote its one too
    assert reports.count("[a.jsession] дневной лимит комментариев — до завтра") == 1  # said once a day


def test_the_comments_stop_a_worker_at_its_daily_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(broadcast_comments, "COMMENTS_LIMITS", str(tmp_path / "comments_limits.json"))
    sends = []

    async def send(session, peer, content, safe_call, report=None, comment_to=None):
        sends.append(content.text)
    monkeypatch.setattr(rich_message, "send", send)

    class _S:
        async def get_me(self):
            return ns(first_name="A")

    @contextlib.asynccontextmanager
    async def ainitialize_session(session):
        yield

    storage = ns(sessions=[], jsessions_paths={}, get_session_path=lambda session: "/w/a.jsession",
                 ainitialize_session=ainitialize_session)
    function = CommentsBroadcastFunc(storage, ns(delay=[0], messages_count=0, comments_per_account_daily=3))

    async def no_delay():
        pass
    function.delay = no_delay
    reports = []

    async def report(text):
        reports.append(text)
    texts = broadcast_comments.variants([RichContent(text=text) for text in ("один", "два")])
    asyncio.run(function.broadcast(_S(), "chan", 1, texts, report))
    assert len(sends) == 3 and set(sends[:2]) == {"один", "два"}  # no variant twice before both have gone
    assert reports[-1] == "[A] дневной лимит комментариев достигнут, стоп"


def test_the_comments_workers_share_the_post_s_variants(monkeypatch):
    """Every worker writes under the one post: between them, no variant twice before every one has gone."""
    sends = []

    async def send(session, peer, content, safe_call, report=None, comment_to=None):
        sends.append(content.text)
    monkeypatch.setattr(rich_message, "send", send)

    class _S:
        async def get_me(self):
            return ns(first_name="A")

    @contextlib.asynccontextmanager
    async def ainitialize_session(session):
        yield

    workers = [_S() for _ in range(3)]
    storage = ns(sessions=workers, jsessions_paths={}, get_session_path=lambda session: None,
                 ainitialize_session=ainitialize_session)
    function = CommentsBroadcastFunc(storage, ns(delay=[0], messages_count=1, comments_per_account_daily=0))

    async def report(text):
        pass
    asyncio.run(function.run("https://t.me/chan/5", [RichContent(text=text) for text in ("один", "два", "три")],
                             report))
    assert sorted(sends) == ["два", "один", "три"]
