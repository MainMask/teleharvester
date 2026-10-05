"""Offline test of the scrape loop with a fake Telethon client (no network)."""

import asyncio
import json
import sys
import time
import types
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from telethon import utils
from telethon.errors import (
    BroadcastForbiddenError, ChannelPrivateError, FloodWaitError, RpcCallFailError,
)
from telethon.tl.types import (
    Channel, ForumTopic, InputPeerChannel, InputPeerUser, PeerChannel, PeerUser, ReactionEmoji, User,
)

import scraper.scrape as scrape
from scraper.config import Credentials
from scraper.scrape import ScrapeParams

_CHANNEL_PEER_ID = utils.get_peer_id(PeerChannel(888))


def _out(tmp_path, kind, ext="parquet"):
    """The single scrape output file for `kind` (posts/reactors/participants), whose
    name now carries the post-date span (`unit.test_<kind>_<from>-<to>.<ext>`)."""
    return next(tmp_path.glob(f"unit.test_{kind}_*.{ext}"))


def _msg(mid, date, text, *, replies=0, empty_thread=False, custom_reaction=False, sender=None,
         reacts=True, can_see_list=None):
    return types.SimpleNamespace(
        id=mid,
        date=date,
        text=text,
        sender_id=getattr(sender, "id", -100),
        sender=sender,
        post_author="Author",
        views=1,
        forwards=0,
        media=False,
        reactions=types.SimpleNamespace(
            results=[types.SimpleNamespace(
                reaction=types.SimpleNamespace(document_id=555) if custom_reaction
                else types.SimpleNamespace(emoticon="👍"),
                count=3,
            )],
            can_see_list=can_see_list,
        ) if reacts else None,
        replies=types.SimpleNamespace(comments=True, replies=replies, channel_id=1001)
        if replies or empty_thread else None,
    )


_BOB_HASH = -8712345678901234567  # beyond float64's exact range


def _reactions_list_response():
    """A fake messages.MessageReactionsList: one user + one channel reactor."""
    return types.SimpleNamespace(
        reactions=[
            types.SimpleNamespace(
                peer_id=PeerUser(777),
                date=datetime(2024, 6, 5, tzinfo=timezone.utc),
                reaction=ReactionEmoji(emoticon="🔥"),
            ),
            types.SimpleNamespace(
                peer_id=PeerChannel(888),
                date=None,
                reaction=ReactionEmoji(emoticon="👍"),
            ),
        ],
        users=[User(id=777, access_hash=_BOB_HASH, username="bob", first_name="Bob", last_name="Ivanov")],
        chats=[Channel(id=888, title="Disc Grp", photo=None, date=None)],
        next_offset=None,
        count=2,
    )


def _main_messages():
    return [
        _msg(40, datetime(2025, 1, 1, tzinfo=timezone.utc), "too new"),
        _msg(30, datetime(2024, 6, 6, tzinfo=timezone.utc), "keep", custom_reaction=True),
        _msg(20, datetime(2024, 6, 5, tzinfo=timezone.utc), None, replies=1),
        _msg(10, datetime(2023, 1, 1, tzinfo=timezone.utc), "too old"),
    ]


def _thread_replies(reply_to):
    if reply_to != 20:
        return []
    return [
        _msg(999, datetime(2024, 6, 5, tzinfo=timezone.utc), "a reply",
             sender=User(id=777, access_hash=_BOB_HASH, username="bob", first_name="Bob", last_name="Ivanov")),
        _msg(998, datetime(2024, 6, 5, tzinfo=timezone.utc), "anon reply",
             sender=Channel(id=888, title="disc", photo=None, date=None), reacts=False),
    ]


class FakeClient:
    comment_ids = {999, 998}
    reaction_peers = []  # peers passed to GetMessageReactionsListRequest; reset per instance
    reaction_ids = []    # message ids passed to GetMessageReactionsListRequest; reset per instance
    calls = []           # (channel, offset_id) for each main-branch iter_messages; reset per instance
    offset_dates = []    # offset_date for each main-branch iter_messages; reset per instance
    reply_calls = []     # reply_to ids passed to iter_messages (GetReplies); reset per instance
    init_kwargs = {}     # kwargs the last instance was constructed with

    def __init__(self, *a, **k):
        type(self).reaction_peers = []
        type(self).reaction_ids = []
        type(self).calls = []
        type(self).offset_dates = []
        type(self).reply_calls = []
        type(self).init_kwargs = k
        self.session = a[0] if a else None

    async def is_user_authorized(self):
        return True

    async def get_me(self):
        return types.SimpleNamespace(id=4242)  # the scraping account

    async def disconnect(self):
        if self.session is not None:  # as Telethon does: the SQLite entity cache is released
            self.session.close()

    async def connect(self):
        return None

    def is_connected(self):
        return True

    async def get_entity(self, arg):
        return types.SimpleNamespace(title="Fake Channel")

    async def get_input_entity(self, arg):
        return arg

    async def get_messages(self, channel, limit=1, offset_date=None):
        msgs = [m for m in _main_messages()
                if offset_date is None or m.date < offset_date]
        return msgs[:limit]

    async def __call__(self, request):
        if type(request).__name__ != "GetMessageReactionsListRequest":
            raise NotImplementedError(request)
        type(self).reaction_peers.append(request.peer)
        type(self).reaction_ids.append(request.id)
        if request.id in self.comment_ids:
            return _reactions_list_response()
        raise BroadcastForbiddenError(request=None)  # channel posts: Telegram says no

    def _main_gen(self, offset_id):
        """Overridable: the main-branch messages an instance yields."""
        async def gen():
            for m in _main_messages():
                if offset_id and m.id >= offset_id:
                    continue
                yield m
        return gen()

    def iter_messages(self, channel, search=None, reply_to=None, offset_id=0, offset_date=None):
        if reply_to is not None:
            type(self).reply_calls.append(reply_to)
            async def replies():
                for m in _thread_replies(reply_to):
                    yield m
            return replies()
        type(self).calls.append((channel, offset_id))
        type(self).offset_dates.append(offset_date)
        return self._main_gen(offset_id)


class FloodThenOkClient(FakeClient):
    """Reaction calls raise FloodWaitError once (a soft ban), then behave normally."""
    flooded = False

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        type(self).flooded = False

    async def __call__(self, request):
        if not type(self).flooded:
            type(self).flooded = True
            raise FloodWaitError(request=None)
        return await super().__call__(request)


class FloodAfterClient(FakeClient):
    """Reactions work for the first call, then every call is a soft ban that never lifts."""
    seen = 0

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        type(self).seen = 0

    async def __call__(self, request):
        type(self).seen += 1
        if type(self).seen > 1:
            raise FloodWaitError(request=None)
        return await super().__call__(request)


class FloodPerPostClient(FakeClient):
    """The first reaction call for post 30 and for comment 999 each hit one soft ban."""
    flooded: set = set()

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        type(self).flooded = set()

    async def __call__(self, request):
        if request.id in {30, 999} - type(self).flooded:
            type(self).flooded.add(request.id)
            raise FloodWaitError(request=None)
        return await super().__call__(request)


class HiddenListClient(FakeClient):
    """A broadcast channel whose posts report reactions.can_see_list=False."""

    def _main_gen(self, offset_id):
        msgs = [
            _msg(30, datetime(2024, 6, 6, tzinfo=timezone.utc), "keep", can_see_list=False),
            _msg(20, datetime(2024, 6, 5, tzinfo=timezone.utc), "with thread",
                 replies=1, can_see_list=False),
            _msg(10, datetime(2023, 1, 1, tzinfo=timezone.utc), "too old"),
        ]

        async def gen():
            for m in msgs:
                if offset_id and m.id >= offset_id:
                    continue
                yield m
        return gen()


@pytest.fixture
def fake_client(monkeypatch):
    monkeypatch.setattr(scrape, "TelegramClient", FakeClient)


def _params(tmp_path, **kw):
    return ScrapeParams(
        channels=kw.pop("channels", ["https://t.me/SomeChannel/"]),
        date_min=datetime(2024, 1, 1, tzinfo=timezone.utc),
        date_max=datetime(2024, 12, 31, 23, 59, 59, tzinfo=timezone.utc),
        name="unit.test",
        out_dir=tmp_path,
        with_reactors=kw.pop("with_reactors", False),
        **kw,
    )


def test_scrape_stores_its_window_for_verify(fake_client, tmp_path):
    path = scrape.run(Credentials(1, "h", ""), _params(tmp_path))
    # the requested window, not the posts' 05.06-06.06 span
    assert pd.read_parquet(path, columns=["Group"]).attrs["scrape_window"] == {
        "date_min": "2024-01-01", "date_max": "2024-12-31"}


def test_rebuilt_participants_keep_the_owner_id(fake_client, tmp_path, capsys):
    from scraper import analysis

    posts = scrape.run(Credentials(1, "h", ""), _params(tmp_path, with_participants=False))
    assert pd.read_parquet(posts, columns=["Group"]).attrs["owner_id"] == 4242

    rebuilt = analysis.participants(str(posts), str(tmp_path / "rebuilt"), "")
    assert set(pd.read_parquet(rebuilt)["Owner ID"]) == {4242}

    df = pd.read_parquet(posts)
    df.attrs.pop("owner_id")  # a posts file scraped before the owner was stored
    df.to_parquet(tmp_path / "old_posts.parquet")
    capsys.readouterr()
    old = analysis.participants(str(tmp_path / "old_posts.parquet"), str(tmp_path / "old"), "")
    assert "Owner ID" not in pd.read_parquet(old).columns
    assert "no Owner ID" in capsys.readouterr().out


def test_combine_takes_a_list_of_files(tmp_path):
    from scraper import analysis

    a, b, skipped = tmp_path / "a_posts.parquet", tmp_path / "b_posts.parquet", tmp_path / "c_posts.parquet"
    for path, ids in ((a, [1, 2]), (b, [2, 3]), (skipped, [9])):
        pd.DataFrame({"Group": "@g", "Message ID": ids, "Date": pd.Timestamp("2024-01-01", tz="UTC"),
                      "Comments": 0}).to_parquet(path)

    out = analysis.combine([str(a), str(b)], str(tmp_path / "Combined_posts"), ["Group", "Message ID"])
    assert sorted(map(int, pd.read_parquet(out)["Message ID"])) == [1, 2, 3]  # c not in the list


def test_group_channel_is_the_inverse_of_channel_ref():
    for raw, channel in [("@name", "@name"), ("-1001629147115", "-1001629147115"),
                         ("https://t.me/+AbC", "https://t.me/+AbC")]:
        group = f"@{scrape._channel_ref(raw).slug}"
        assert scrape.group_channel(group) == channel
        assert scrape._channel_ref(scrape.group_channel(group)).slug == group[1:]


def test_scrape_end_to_end(fake_client, tmp_path, capsys):
    path = scrape.run(Credentials(1, "hash", ""), _params(tmp_path))
    assert path.name == "unit.test_posts_05.06.2024-06.06.2024.parquet"  # kept posts span

    log = capsys.readouterr().out
    assert "%" in log and "ETA" in log and ("█" in log or "░" in log)  # progress bar

    df = pd.read_parquet(path)
    assert list(df["Message ID"]) == ["30", "20"]        # newer skipped, older breaks
    assert list(df["Comments"]) == [0, 2]                # post 30 no thread, post 20 two replies
    assert set(df["Group"]) == {"@SomeChannel"}          # URL form -> slug
    assert df.iloc[0]["Url"] == "https://t.me/SomeChannel/30"
    assert df.iloc[1]["Content"] == ""                   # None text -> ""
    assert "[custom:" in df.iloc[0]["Reactions"]
    assert '"Comment Content": "a reply"' in df.iloc[1]["Comments List"]  # post 20 had a thread
    assert '"Comment Author Username": "bob"' in df.iloc[1]["Comments List"]
    assert '"Comment Author Name": "Bob Ivanov"' in df.iloc[1]["Comments List"]
    assert '"Comment Author Username": "[channel]"' in df.iloc[1]["Comments List"]  # anon reply
    assert f'"Comment Author Access Hash": {_BOB_HASH}' in df.iloc[1]["Comments List"]
    assert df.iloc[0]["Comments List"] == "[]"           # post 30 had no thread

    people = pd.read_parquet(_out(tmp_path, "participants")).set_index("ID")
    assert people.loc[777, "Username"] == "bob"
    assert people.loc[777, "Name"] == "Bob Ivanov"
    assert people.loc[777, "Comments"] == 1
    assert people.loc[777, "Access Hash"] == _BOB_HASH
    assert set(people["Owner ID"]) == {4242}  # the scraping account owns the hashes


def test_progress_fraction_tracks_message_id_range(fake_client, tmp_path, capsys, monkeypatch):
    # _main_messages within [2024-01-01, 2024-12-31]: id_hi=30, id_lo=10, span=20.
    monkeypatch.setattr(scrape, "PRINT_EVERY", 0)  # a line per post
    scrape.run(Credentials(1, "h", ""), _params(tmp_path))
    lines = [ln for ln in capsys.readouterr().out.splitlines() if "% " in ln and "id " in ln]
    assert " 0.0%" in lines[0] and "id 30" in lines[0]      # first kept post: at id_hi
    assert " 50.0%" in lines[1] and "id 20" in lines[1]     # halfway down the id range


def test_progress_lines_are_throttled(fake_client, tmp_path, capsys):
    # both posts come within PRINT_EVERY: journald gets one line, not one per post
    scrape.run(Credentials(1, "h", ""), _params(tmp_path))
    lines = [ln for ln in capsys.readouterr().out.splitlines() if "% " in ln and "id " in ln]
    assert len(lines) == 1 and "id 30" in lines[0]


def test_on_progress_gets_fraction_eta_and_posts(fake_client, tmp_path):
    calls = []
    scrape.run(Credentials(1, "h", ""), _params(tmp_path, on_progress=lambda *a: calls.append(a)))
    assert calls == [(0.0, None, 1), (0.5, None, 2)]  # ETA still "estimating" in the first 30 s


def test_scrape_no_comments_flag(fake_client, tmp_path):
    df = pd.read_parquet(scrape.run(Credentials(1, "h", ""), _params(tmp_path, with_comments=False)))
    assert list(df["Comments List"]) == ["[]", "[]"]


def test_scrape_numeric_channel_id(fake_client, tmp_path, capsys):
    path = scrape.run(Credentials(1, "h", ""), _params(tmp_path, channels=["-1001629147115"]))
    df = pd.read_parquet(path)
    assert set(df["Group"]) == {"@c1629147115"}
    assert df.iloc[0]["Url"] == "https://t.me/c/1629147115/30"
    assert (tmp_path / "unit.test_partial" / "c1629147115_until_00002.parquet").exists()
    assert '"Fake Channel" (-1001629147115)' in capsys.readouterr().out  # title in the header


def test_scrape_reactors(fake_client, tmp_path):
    scrape.run(Credentials(1, "h", ""), _params(tmp_path, with_reactors=True))

    files = list(tmp_path.glob("unit.test_reactors_*.parquet"))
    assert len(files) == 1
    r = pd.read_parquet(files[0])

    # channel posts are 403; only comment 999 carried reactions (998 had none)
    assert set(r["Target"]) == {"comment"}
    assert set(r["Message ID"]) == {999}
    assert set(r["Post ID"]) == {20}
    assert (r["Url"] == "https://t.me/SomeChannel/20?comment=999").all()

    by_id = r.set_index("Reactor ID")
    assert by_id.loc[777, "Reactor Username"] == "bob"
    assert by_id.loc[777, "Reactor Name"] == "Bob Ivanov"
    assert by_id.loc[777, "Reaction"] == "🔥"
    assert by_id.loc[777, "Date"] == "2024-06-05 00:00:00"
    assert by_id.loc[_CHANNEL_PEER_ID, "Reactor Username"] == "[channel]"
    assert by_id.loc[_CHANNEL_PEER_ID, "Reactor Name"] == "Disc Grp"
    assert str(r["Reactor Access Hash"].dtype) == "Int64"
    assert by_id.loc[777, "Reactor Access Hash"] == _BOB_HASH
    assert pd.isna(by_id.loc[_CHANNEL_PEER_ID, "Reactor Access Hash"])

    people = pd.read_parquet(_out(tmp_path, "participants")).set_index("ID")
    assert people.loc[777, "Reactions"] >= 1             # reactor folded into participants

    # comment reactions must target the discussion group (replies.channel_id), not
    # the broadcast channel — otherwise Telegram answers BroadcastForbiddenError
    assert any(getattr(p, "channel_id", None) == 1001 for p in FakeClient.reaction_peers)


def test_scrape_reactors_skips_hidden_list(monkeypatch, tmp_path):
    monkeypatch.setattr(scrape, "TelegramClient", HiddenListClient)
    scrape.run(Credentials(1, "h", ""),
               _params(tmp_path, with_reactors=True, with_participants=False))

    # posts 30 and 20 report can_see_list=False -> the reactor list is never
    # requested; only the linked thread's comment (can_see_list unset) is fetched
    assert HiddenListClient.reaction_ids == [999]
    r = pd.read_parquet(_out(tmp_path, "reactors"))
    assert set(r["Message ID"]) == {999}




def test_flood_wait_is_waited_out_then_resumes(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(scrape, "TelegramClient", FloodThenOkClient)
    monkeypatch.setattr(scrape, "FLOOD_RETRY_BUFFER", 0)

    path = scrape.run(Credentials(1, "h", ""),
                      _params(tmp_path, with_reactors=True, with_participants=False))

    assert list(pd.read_parquet(path)["Message ID"]) == ["30", "20"]  # nothing skipped
    r = pd.read_parquet(_out(tmp_path, "reactors"))
    assert set(r["Message ID"]) == {999}                  # reactors collected after the wait
    assert "FLOOD_WAIT" in capsys.readouterr().out


def test_persistent_flood_stops_with_resume_hint(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(scrape, "TelegramClient", FloodAfterClient)
    monkeypatch.setattr(scrape, "FLOOD_RETRY_BUFFER", 0)
    monkeypatch.setattr(scrape, "FLOOD_MAX_ATTEMPTS", 2)

    with pytest.raises(SystemExit):
        scrape.run(Credentials(1, "h", ""), _params(tmp_path, with_reactors=True))

    out = capsys.readouterr().out
    assert "FLOOD_WAIT" in out and "with the same name" in out
    assert (_ckpt(tmp_path) / "resume.json").exists()     # checkpoint left for a resume


def test_flood_attempts_reset_after_progress(monkeypatch, tmp_path):
    monkeypatch.setattr(scrape, "TelegramClient", FloodPerPostClient)
    monkeypatch.setattr(scrape, "FLOOD_RETRY_BUFFER", 0)
    monkeypatch.setattr(scrape, "FLOOD_MAX_ATTEMPTS", 1)

    # two bans, but post 30 was saved in between -> not "too many in a row"
    path = scrape.run(Credentials(1, "h", ""),
                      _params(tmp_path, with_reactors=True, with_participants=False))
    assert list(pd.read_parquet(path)["Message ID"]) == ["30", "20"]


def test_scrape_reactors_without_comments(fake_client, tmp_path):
    # only per-post attempts happen (all 403); must not crash, no reactors file
    scrape.run(Credentials(1, "h", ""), _params(tmp_path, with_reactors=True, with_comments=False))
    assert not list(tmp_path.glob("*reactors*"))
    assert not list(tmp_path.glob("*participants*"))      # nothing to build -> skipped


# --- connection resilience + resume -----------------------------------------

class FlakyClient(FakeClient):
    """Drops the connection once, mid-iteration, then recovers."""
    recovered = False

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        type(self).recovered = False

    def _main_gen(self, offset_id):
        async def gen():
            for m in _main_messages():
                if offset_id and m.id >= offset_id:
                    continue
                yield m
                if m.id == 30 and not type(self).recovered:
                    type(self).recovered = True
                    raise ConnectionError("boom")
        return gen()


class DeadClient(FakeClient):
    """Never gets past the first post."""

    def _main_gen(self, offset_id):
        async def gen():
            if not offset_id:
                yield _msg(40, datetime(2025, 1, 1, tzinfo=timezone.utc), "too new")
                yield _msg(30, datetime(2024, 6, 6, tzinfo=timezone.utc), "keep")
            raise ConnectionError("dead")
        return gen()


class ResumeClient(FakeClient):
    """After a resume (offset_id set) one extra older post appears."""

    def _main_gen(self, offset_id):
        async def gen():
            if offset_id:
                yield _msg(15, datetime(2024, 6, 4, tzinfo=timezone.utc), "resumed extra")
            for m in _main_messages():
                if offset_id and m.id >= offset_id:
                    continue
                yield m
        return gen()


class CtrlCClient(FakeClient):
    """User hits Ctrl-C after the first post."""

    def _main_gen(self, offset_id):
        async def gen():
            yield _msg(40, datetime(2025, 1, 1, tzinfo=timezone.utc), "too new")
            yield _msg(30, datetime(2024, 6, 6, tzinfo=timezone.utc), "keep")
            raise KeyboardInterrupt
        return gen()


class CancelClient(FakeClient):
    """asyncio cancels the task mid-run - what a real SIGINT does."""

    def _main_gen(self, offset_id):
        async def gen():
            yield _msg(40, datetime(2025, 1, 1, tzinfo=timezone.utc), "too new")
            yield _msg(30, datetime(2024, 6, 6, tzinfo=timezone.utc), "keep")
            raise asyncio.CancelledError
        return gen()


class ReactorDropClient(FakeClient):
    """The connection drops once, inside the per-message reactions call."""
    dropped = False

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        type(self).dropped = False

    async def __call__(self, request):
        if not type(self).dropped:
            type(self).dropped = True
            raise ConnectionError("boom during reactions")
        return await super().__call__(request)


class ServerErrorDuringReactionsClient(FakeClient):
    """A transient 500 (RPC_CALL_FAIL) lands once, inside the per-message reactions call."""
    failed = False

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        type(self).failed = False

    async def __call__(self, request):
        if not type(self).failed:
            type(self).failed = True
            raise RpcCallFailError(request=None)
        return await super().__call__(request)


class ServerErrorAfterClient(FakeClient):
    """Reactions work for the first call, then every call is a 500 that never clears."""
    seen = 0

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        type(self).seen = 0

    async def __call__(self, request):
        type(self).seen += 1
        if type(self).seen > 1:
            raise RpcCallFailError(request=None)
        return await super().__call__(request)


def _partial(tmp_path):
    return tmp_path / "unit.test_partial"


def _ckpt(tmp_path):
    return _partial(tmp_path) / "checkpoint"


def _resume_meta(tmp_path, **over):
    meta = {
        "name": "unit.test",
        "channels": ["https://t.me/SomeChannel/"],
        "keyword": "",
        "date_min": datetime(2024, 1, 1, tzinfo=timezone.utc).isoformat(),
        "date_max": datetime(2024, 12, 31, 23, 59, 59, tzinfo=timezone.utc).isoformat(),
        "channel_index": 0,
        "last_id": 0,
        "t_index": 0,
    }
    meta.update(over)
    return meta


def _seed_checkpoint(tmp_path, rows, meta):
    d = _ckpt(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    if rows:
        pd.DataFrame(rows).to_parquet(d / "posts_part_00000.parquet")
    (d / "resume.json").write_text(json.dumps(meta), encoding="utf-8")


_CK_ROW_30 = {"Type": "text", "Group": "@SomeChannel", "Content": "c30",
              "Date": "2024-06-06 00:00:00", "Message ID": 30, "Comments List": "[]",
              "Url": "https://t.me/SomeChannel/30"}
_CK_ROW_20 = {"Type": "text", "Group": "@SomeChannel", "Content": "c20",
              "Date": "2024-06-05 00:00:00", "Message ID": 20, "Comments List": "[]",
              "Url": "https://t.me/SomeChannel/20"}


def test_connection_kwargs_passed(fake_client, tmp_path):
    scrape.run(Credentials(1, "h", ""), _params(tmp_path))
    k = FakeClient.init_kwargs
    assert k["connection_retries"] == scrape.CONNECTION_RETRIES
    assert k["retry_delay"] == scrape.RETRY_DELAY
    assert k["request_retries"] == scrape.REQUEST_RETRIES
    assert k["flood_sleep_threshold"] == scrape.FLOOD_SLEEP_THRESHOLD


def test_scrape_default_offset_id_zero(fake_client, tmp_path):
    scrape.run(Credentials(1, "h", ""), _params(tmp_path))
    assert FakeClient.calls[0][1] == 0


def test_scrape_resumes_after_connection_error(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(scrape, "TelegramClient", FlakyClient)
    monkeypatch.setattr(scrape, "RESUME_BASE_WAIT", 0)
    path = scrape.run(Credentials(1, "h", ""), _params(tmp_path, with_reactors=True))

    df = pd.read_parquet(path)
    assert list(df["Message ID"]) == ["30", "20"]        # no duplicate 30
    assert len(FlakyClient.calls) == 2
    assert FlakyClient.calls[1][1] == 30                  # restarted just past the last saved id
    assert "retry 1/" in capsys.readouterr().out

    r = pd.read_parquet(_out(tmp_path, "reactors"))
    assert set(r["Message ID"]) == {999}                  # comment reactors collected once
    assert len(r) == 2 and not r.duplicated().any()


def test_scrape_gives_up_and_hints_resume(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(scrape, "TelegramClient", DeadClient)
    monkeypatch.setattr(scrape, "RESUME_BASE_WAIT", 0)
    monkeypatch.setattr(scrape, "RESUME_MAX_ATTEMPTS", 2)

    with pytest.raises(SystemExit):
        scrape.run(Credentials(1, "h", ""), _params(tmp_path))

    out = capsys.readouterr().out
    assert "with the same name" in out
    meta = json.loads((_ckpt(tmp_path) / "resume.json").read_text())
    assert meta["last_id"] == 30 and meta["channel_index"] == 0
    assert len(pd.read_parquet(_ckpt(tmp_path) / "posts_part_00000.parquet")) == 1


def test_keyboard_interrupt_checkpoints_and_hints(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(scrape, "TelegramClient", CtrlCClient)

    with pytest.raises(SystemExit):
        scrape.run(Credentials(1, "h", ""), _params(tmp_path))

    out = capsys.readouterr().out
    assert "with the same name" in out
    meta = json.loads((_ckpt(tmp_path) / "resume.json").read_text())
    assert meta["last_id"] == 30


def test_async_cancel_checkpoints_and_hints(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(scrape, "TelegramClient", CancelClient)

    with pytest.raises(SystemExit):
        scrape.run(Credentials(1, "h", ""), _params(tmp_path))

    out = capsys.readouterr().out
    assert "with the same name" in out
    meta = json.loads((_ckpt(tmp_path) / "resume.json").read_text())
    assert meta["last_id"] == 30


def test_sigterm_checkpoints_and_hints(monkeypatch, tmp_path, capsys):
    import os
    import signal

    class SigtermClient(FakeClient):
        """systemctl stop / docker stop: a SIGTERM lands after the first saved post."""

        def _main_gen(self, offset_id):
            async def gen():
                yield _msg(40, datetime(2025, 1, 1, tzinfo=timezone.utc), "too new")
                yield _msg(30, datetime(2024, 6, 6, tzinfo=timezone.utc), "keep")
                os.kill(os.getpid(), signal.SIGTERM)  # default action would kill the process
                await asyncio.sleep(0.1)              # let the loop deliver it -> task.cancel()
                yield _msg(20, datetime(2024, 6, 5, tzinfo=timezone.utc), "must not reach")
            return gen()

    monkeypatch.setattr(scrape, "TelegramClient", SigtermClient)

    # The process surviving this call at all proves _scrape installed the SIGTERM
    # handler (otherwise the os.kill above would terminate pytest).
    with pytest.raises(SystemExit):
        scrape.run(Credentials(1, "h", ""), _params(tmp_path))

    out = capsys.readouterr().out
    assert "with the same name" in out
    meta = json.loads((_ckpt(tmp_path) / "resume.json").read_text())
    assert meta["last_id"] == 30  # post 30 saved; post 20 never reached








def test_connection_error_during_reactions_triggers_retry(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(scrape, "TelegramClient", ReactorDropClient)
    monkeypatch.setattr(scrape, "RESUME_BASE_WAIT", 0)

    path = scrape.run(Credentials(1, "h", ""), _params(tmp_path, with_reactors=True))

    assert list(pd.read_parquet(path)["Message ID"]) == ["30", "20"]  # nothing skipped
    assert "retry 1/" in capsys.readouterr().out
    assert len(ReactorDropClient.calls) == 2                          # channel restarted once


def test_server_error_during_reactions_triggers_retry(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(scrape, "TelegramClient", ServerErrorDuringReactionsClient)
    monkeypatch.setattr(scrape, "RESUME_BASE_WAIT", 0)

    path = scrape.run(Credentials(1, "h", ""), _params(tmp_path, with_reactors=True))

    assert list(pd.read_parquet(path)["Message ID"]) == ["30", "20"]  # thread redone, not truncated
    assert "retry 1/" in capsys.readouterr().out
    assert len(ServerErrorDuringReactionsClient.calls) == 2           # channel restarted once


def test_persistent_server_error_stops_with_resume_hint(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(scrape, "TelegramClient", ServerErrorAfterClient)
    monkeypatch.setattr(scrape, "RESUME_BASE_WAIT", 0)
    monkeypatch.setattr(scrape, "RESUME_MAX_ATTEMPTS", 2)

    with pytest.raises(SystemExit):
        scrape.run(Credentials(1, "h", ""), _params(tmp_path, with_reactors=True))

    out = capsys.readouterr().out
    assert "with the same name" in out
    assert (_ckpt(tmp_path) / "resume.json").exists()                 # checkpoint left for a resume


def test_resume_flag_reloads_checkpoint(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(scrape, "TelegramClient", ResumeClient)
    _seed_checkpoint(tmp_path, [_CK_ROW_30, _CK_ROW_20],
                     _resume_meta(tmp_path, last_id=20, t_index=2))

    path = scrape.run(Credentials(1, "h", ""), _params(tmp_path, resume=True))

    assert "Resuming" in capsys.readouterr().out
    assert ResumeClient.calls[0][1] == 20
    assert list(pd.read_parquet(path)["Message ID"]) == ["30", "20", "15"]


def test_resume_flag_skips_completed_channel(monkeypatch, tmp_path):
    monkeypatch.setattr(scrape, "TelegramClient", FakeClient)
    chans = ["@one", "@two"]
    _seed_checkpoint(tmp_path, [_CK_ROW_30],
                     _resume_meta(tmp_path, channels=chans, channel_index=1, t_index=1))

    scrape.run(Credentials(1, "h", ""), _params(tmp_path, resume=True, channels=chans))
    assert [c[0] for c in FakeClient.calls] == ["@two"]


def test_resume_flag_param_mismatch(monkeypatch, tmp_path):
    monkeypatch.setattr(scrape, "TelegramClient", FakeClient)
    _seed_checkpoint(tmp_path, [_CK_ROW_30], _resume_meta(tmp_path, channels=["@old"]))

    with pytest.raises(SystemExit, match="does not match"):
        scrape.run(Credentials(1, "h", ""), _params(tmp_path, resume=True))


def test_resume_flag_missing_json(fake_client, tmp_path, capsys):
    path = scrape.run(Credentials(1, "h", ""), _params(tmp_path, resume=True))
    assert "not found" in capsys.readouterr().out
    assert list(pd.read_parquet(path)["Message ID"]) == ["30", "20"]


def test_scrape_success_clears_resume_json(fake_client, tmp_path):
    path = scrape.run(Credentials(1, "h", ""), _params(tmp_path))
    assert path.exists()
    assert not (_ckpt(tmp_path) / "resume.json").exists()




def test_checkpoint_writes_incremental_shards(fake_client, tmp_path, monkeypatch):
    monkeypatch.setattr(scrape, "CHECKPOINT_EVERY", 1)
    path = scrape.run(Credentials(1, "h", ""), _params(tmp_path, with_participants=False))
    # run() clears checkpoint/ on success, so inspect the per-channel snapshot +
    # the final file: both must carry every scraped post despite the tiny flushes
    assert list(pd.read_parquet(path)["Message ID"]) == ["30", "20"]
    snap = next((tmp_path / "unit.test_partial").glob("*_until_*.parquet"))
    assert sorted(pd.read_parquet(snap)["Message ID"].astype(str)) == ["20", "30"]


def test_fresh_run_clears_stale_shards(fake_client, tmp_path):
    d = _ckpt(tmp_path)
    d.mkdir(parents=True)
    pd.DataFrame([{**_CK_ROW_30, "Message ID": 777, "Url": "u"}]).to_parquet(
        d / "posts_part_00000.parquet")  # leftover from a previous job, same --name

    path = scrape.run(Credentials(1, "h", ""), _params(tmp_path, with_participants=False))
    assert list(pd.read_parquet(path)["Message ID"]) == ["30", "20"]  # 777 not pulled in


def test_clean_finish_removes_checkpoint_dir_contents(fake_client, tmp_path):
    scrape.run(Credentials(1, "h", ""), _params(tmp_path, with_participants=False))
    d = _ckpt(tmp_path)
    assert not list(d.glob("*.parquet"))
    assert not (d / "resume.json").exists()


def test_resume_flag_missing_checkpoint_refuses(monkeypatch, tmp_path):
    monkeypatch.setattr(scrape, "TelegramClient", FakeClient)
    d = _ckpt(tmp_path)
    d.mkdir(parents=True)
    (d / "resume.json").write_text(json.dumps(_resume_meta(tmp_path, t_index=500, last_id=42)))

    with pytest.raises(SystemExit, match="missing"):
        scrape.run(Credentials(1, "h", ""), _params(tmp_path, resume=True))


def _reactor_row(mid, rid, reaction, group="@c", target="post"):
    return {"Type": "reactor", "Target": target, "Group": group, "Message ID": mid,
            "Post ID": mid, "Url": "u", "Reactor ID": rid, "Reactor Username": "",
            "Reactor Name": "", "Reaction": reaction, "Date": "d"}


def test_run_dedups_reactor_rows(monkeypatch, tmp_path):
    posts = pd.DataFrame([{"Type": "text", "Group": "@c", "Message ID": 1,
                           "Date": "2024-01-01 00:00:00", "Comments List": "[]", "Url": "u"}])

    async def fake_scrape(creds, params):
        d = _ckpt(tmp_path)
        d.mkdir(parents=True, exist_ok=True)
        posts.to_parquet(d / "posts_part_00000.parquet")
        # shard 0: a within-shard repeat + two distinct reactions for msg 1
        pd.DataFrame([_reactor_row(1, 7, "🔥"), _reactor_row(1, 7, "🔥"),
                      _reactor_row(1, 8, "👍")]).to_parquet(d / "reactors_part_00000.parquet")
        # shard 1: msg 1 re-scraped after a resume (same rows) + a new msg 2
        pd.DataFrame([_reactor_row(1, 7, "🔥"), _reactor_row(1, 8, "👍"),
                      _reactor_row(2, 9, "❤")]).to_parquet(d / "reactors_part_00001.parquet")

    monkeypatch.setattr(scrape, "_scrape", fake_scrape)
    scrape.run(Credentials(1, "h", ""), _params(tmp_path, with_reactors=True, with_participants=False))
    r = pd.read_parquet(_out(tmp_path, "reactors"))
    assert len(r) == 3                                     # (1,7,🔥) (1,8,👍) (2,9,❤)
    assert not r.duplicated().any()
    assert sorted(r["Message ID"]) == [1, 1, 2]


def test_collect_reactors_drops_min_user_hash(monkeypatch):
    monkeypatch.setattr(scrape, "REACTOR_CALL_DELAY", 0)
    res = types.SimpleNamespace(
        reactions=[types.SimpleNamespace(peer_id=PeerUser(uid), date=None,
                                         reaction=ReactionEmoji(emoticon="👍"))
                   for uid in (777, 555)],
        users=[User(id=777, access_hash=_BOB_HASH, username="bob"),
               User(id=555, access_hash=123, min=True, username="minnie")],  # hash unusable
        chats=[], next_offset=None, count=2,
    )

    async def client(request):
        return res

    ref = scrape._channel_ref("https://t.me/SomeChannel/")
    msg = _msg(20, datetime(2024, 6, 5, tzinfo=timezone.utc), "body")
    rows = asyncio.run(scrape._collect_reactors(client, ref.arg, ref, 20, msg, "post"))
    hashes = {r["Reactor ID"]: r["Reactor Access Hash"] for r in rows}
    assert hashes == {777: _BOB_HASH, 555: None}


def test_bots_and_deleted_accounts_have_no_usable_hash(monkeypatch):
    assert scrape._user_access_hash(User(id=1, access_hash=11)) == 11
    assert scrape._user_access_hash(User(id=2, access_hash=22, bot=True)) is None
    assert scrape._user_access_hash(User(id=3, access_hash=33, deleted=True)) is None

    monkeypatch.setattr(scrape, "REACTOR_CALL_DELAY", 0)
    res = types.SimpleNamespace(
        reactions=[types.SimpleNamespace(peer_id=PeerUser(uid), date=None,
                                         reaction=ReactionEmoji(emoticon="👍"))
                   for uid in (777, 888)],
        users=[User(id=777, access_hash=_BOB_HASH, username="bob"),
               User(id=888, access_hash=123, bot=True, username="modbot")],  # no one to mail
        chats=[], next_offset=None, count=2,
    )

    async def client(request):
        return res

    ref = scrape._channel_ref("https://t.me/SomeGroup/")
    msg = _msg(20, datetime(2024, 6, 5, tzinfo=timezone.utc), "body")
    rows = asyncio.run(scrape._collect_reactors(client, ref.arg, ref, 20, msg, "post"))
    assert {r["Reactor ID"]: r["Reactor Access Hash"] for r in rows} == {777: _BOB_HASH, 888: None}


def test_collect_reactors_user_and_channel_with_same_id(monkeypatch):
    monkeypatch.setattr(scrape, "REACTOR_CALL_DELAY", 0)
    res = types.SimpleNamespace(
        reactions=[types.SimpleNamespace(peer_id=peer, date=None,
                                         reaction=ReactionEmoji(emoticon="👍"))
                   for peer in (PeerUser(5), PeerChannel(5))],
        users=[User(id=5, access_hash=_BOB_HASH, username="bob", first_name="Bob")],
        chats=[Channel(id=5, title="Chan", photo=None, date=None)],
        next_offset=None, count=2,
    )

    async def client(request):
        return res

    ref = scrape._channel_ref("https://t.me/SomeChannel/")
    msg = _msg(20, datetime(2024, 6, 5, tzinfo=timezone.utc), "body")
    rows = asyncio.run(scrape._collect_reactors(client, ref.arg, ref, 20, msg, "post"))
    user, chan = rows
    assert (user["Reactor Username"], user["Reactor Name"], user["Reactor Access Hash"]) == (
        "bob", "Bob", _BOB_HASH)                        # not overwritten by channel 5
    assert chan["Reactor Name"] == "Chan"


def test_consolidate_reactors_streaming(tmp_path):
    d = tmp_path / "ckpt"
    d.mkdir()
    pd.DataFrame([_reactor_row(10, 1, "🔥"), _reactor_row(10, 1, "🔥"),   # within-shard dup
                  _reactor_row(10, 2, "👍")]).to_parquet(d / "reactors_part_00000.parquet")
    pd.DataFrame([_reactor_row(10, 1, "🔥"), _reactor_row(10, 2, "👍")]   # msg 10 re-scraped
                 ).to_parquet(d / "reactors_part_00001.parquet")
    pd.DataFrame([_reactor_row(11, 3, "❤")]).to_parquet(d / "reactors_part_00002.parquet")

    dest, n = scrape._consolidate_reactors(d, tmp_path / "out.parquet")
    out = pd.read_parquet(dest)
    assert n == len(out) == 3
    assert {(g, m, i, x) for g, m, i, x in
            zip(out["Group"], out["Message ID"], out["Reactor ID"], out["Reaction"])} == {
        ("@c", 10, 1, "🔥"), ("@c", 10, 2, "👍"), ("@c", 11, 3, "❤")}


def test_write_snapshot_stitches_mismatched_schemas(tmp_path):
    # real scrapes produce shards whose all-None columns (Author/Views/Shares on
    # unsigned or non-broadcast posts) infer a different parquet type per batch
    d = tmp_path / "ckpt"
    d.mkdir()
    pd.DataFrame([{"Message ID": 1, "Author": None, "Views": None},
                  {"Message ID": 2, "Author": None, "Views": None}]
                 ).to_parquet(d / "posts_part_00000.parquet")
    pd.DataFrame([{"Message ID": 3, "Author": "Signed", "Views": 55}]
                 ).to_parquet(d / "posts_part_00001.parquet")

    scrape._write_snapshot(d, 0, tmp_path / "snap.parquet")  # shard by shard, one schema
    df = pd.read_parquet(tmp_path / "snap.parquet")
    assert sorted(df["Message ID"]) == [1, 2, 3]
    assert df.set_index("Message ID").loc[3, "Author"] == "Signed"
    assert df["Content"].isna().all()  # a column the shards don't have comes in null


def test_eta_uses_current_channel_time(fake_client, tmp_path, monkeypatch, capsys):
    clock = [0.0]

    def _monotonic():
        clock[0] += 20  # every clock read moves time forward
        return clock[0]

    async def _nosleep(*a, **k):
        return None

    monkeypatch.setattr(scrape.time, "monotonic", _monotonic)
    monkeypatch.setattr(scrape.asyncio, "sleep", _nosleep)  # skip the 60s/channel pause
    scrape.run(Credentials(1, "h", ""), _params(tmp_path, channels=["@a", "@b"],
                                            with_participants=False))
    etas = [line.split("ETA ")[1] for line in capsys.readouterr().out.splitlines()
            if "| id 20 |" in line]
    # both channels scrape the same ids at the same pace: the earlier channel's time
    # must not inflate the second channel's ETA
    assert len(etas) == 2 and etas[0] == etas[1] != "estimating"


def test_until_snapshot_is_incremental(fake_client, tmp_path, monkeypatch):
    async def _nosleep(*a, **k):
        return None

    monkeypatch.setattr(scrape.asyncio, "sleep", _nosleep)  # skip the 60s/channel pause

    seen_starts = []
    real = scrape._write_snapshot

    def spy(ckpt_dir, start, dest):
        seen_starts.append(start)
        return real(ckpt_dir, start, dest)

    monkeypatch.setattr(scrape, "_write_snapshot", spy)
    scrape.run(Credentials(1, "h", ""), _params(tmp_path, channels=["@a", "@b"],
                                            with_participants=False))
    # channel @a snapshots from 0; channel @b must not re-read @a's shards
    assert seen_starts[0] == 0 and seen_starts[1] > 0


def test_combine_ignores_resume_checkpoint(tmp_path):
    from scraper import analysis

    pdir = _partial(tmp_path)
    (pdir / "checkpoint").mkdir(parents=True)
    pd.DataFrame([_CK_ROW_30, _CK_ROW_20]).to_parquet(pdir / "SomeChannel_until_00002.parquet")
    pd.DataFrame([{  # reactor-shaped row that must NOT be pulled into a post merge
        "Type": "reactor", "Target": "comment", "Group": "@SomeChannel", "Message ID": 999,
        "Post ID": 20, "Url": "u", "Reactor ID": 7, "Reaction": "🔥", "Date": "2024-06-05 00:00:00",
    }]).to_parquet(pdir / "checkpoint" / "reactors.parquet")

    out = tmp_path / "combined.parquet"
    analysis.combine(str(pdir), str(out), ["Group", "Message ID"])
    assert sorted(pd.read_parquet(out)["Message ID"]) == ["20", "30"]  # reactor row 999 excluded


def test_collect_post_returns_row_and_reactor_rows(tmp_path):
    ref = scrape._channel_ref("https://t.me/SomeChannel/")
    msg = _msg(20, datetime(2024, 6, 5, tzinfo=timezone.utc), "body", replies=1)
    row, reactors = asyncio.run(
        scrape._collect_post(FakeClient(), ref, msg, _params(tmp_path, with_reactors=True))
    )
    assert row["Message ID"] == 20 and row["Group"] == "@SomeChannel"
    assert row["Url"] == "https://t.me/SomeChannel/20"
    assert json.loads(row["Comments List"])[0]["Comment Author Username"] == "bob"
    assert {r["Message ID"] for r in reactors} == {999} and len(reactors) == 2


def test_empty_thread_old_post_skips_getreplies(tmp_path):
    # broadcast post, comments enabled, counter 0, settled: no GetReplies call
    ref = scrape._channel_ref("https://t.me/SomeChannel/")
    msg = _msg(20, datetime(2024, 6, 5, tzinfo=timezone.utc), "body", empty_thread=True)
    client = FakeClient()
    row, _ = asyncio.run(scrape._collect_post(client, ref, msg, _params(tmp_path)))
    assert client.reply_calls == []
    assert row["Comments List"] == "[]"


def test_empty_thread_fresh_post_still_fetched(tmp_path):
    # same, but the post is minutes old — the 0 counter may just be lagging, so fetch
    ref = scrape._channel_ref("https://t.me/SomeChannel/")
    msg = _msg(20, datetime.now(timezone.utc), "body", empty_thread=True)
    client = FakeClient()
    row, _ = asyncio.run(scrape._collect_post(client, ref, msg, _params(tmp_path)))
    assert client.reply_calls == [20]
    assert json.loads(row["Comments List"])[0]["Comment Author Username"] == "bob"


class _DialogsClient:
    """Knows a numeric-ID channel only after get_dialogs() filled the cache."""

    def __init__(self, cached: bool, miss=ValueError):
        self.cached, self.miss, self.dialog_calls = cached, miss, 0

    async def get_input_entity(self, arg):
        if not self.cached:
            raise self.miss("uncached")
        return arg

    async def get_dialogs(self):
        self.dialog_calls += 1
        self.cached = True


@pytest.mark.parametrize("channel, cached, miss, dialog_calls", [
    ("-1001629147115", False, ValueError, 1),  # private channel by ID, cold cache -> load dialogs
    ("-1001629147115", False, ChannelPrivateError, 1),  # probe refused instead of "not found"
    ("-1001629147115", True, ValueError, 0),   # already cached -> no extra request
    ("@name", False, ValueError, 0),           # usernames resolve on their own
])
def test_warm_channel_loads_dialogs_only_on_id_miss(channel, cached, miss, dialog_calls):
    client = _DialogsClient(cached, miss)
    loaded = asyncio.run(scrape._warm_channel(client, scrape._channel_ref(channel), False))
    assert client.dialog_calls == dialog_calls and loaded == bool(dialog_calls)


def test_warm_channel_loads_dialogs_once_per_run():
    client = _DialogsClient(cached=False)

    async def dialogs_without_caching():  # e.g. IDs of channels the account is not in
        client.dialog_calls += 1

    client.get_dialogs = dialogs_without_caching
    loaded = False
    for channel in ("-1001", "-1002", "-1003"):
        loaded = asyncio.run(scrape._warm_channel(client, scrape._channel_ref(channel), loaded))
    assert client.dialog_calls == 1


def test_consolidate_reactors_keeps_post_and_comment_with_same_id(tmp_path):
    # a channel post and a discussion comment live in different id spaces
    d = tmp_path / "ckpt"
    d.mkdir()
    pd.DataFrame([_reactor_row(500, 1, "🔥"), _reactor_row(500, 1, "🔥", target="comment")]
                 ).to_parquet(d / "reactors_part_00000.parquet")
    pd.DataFrame([_reactor_row(501, 1, "🔥", target="comment")]).to_parquet(
        d / "reactors_part_00001.parquet")
    pd.DataFrame([_reactor_row(501, 1, "🔥")]).to_parquet(d / "reactors_part_00002.parquet")

    _, n = scrape._consolidate_reactors(d, tmp_path / "out.parquet")
    assert n == 4


def test_comment_text_keeps_apostrophes(tmp_path):
    class ApostropheClient(FakeClient):
        def iter_messages(self, channel, reply_to=None, **k):
            if reply_to is None:
                return super().iter_messages(channel, **k)

            async def replies():
                yield _msg(999, datetime(2024, 6, 5, tzinfo=timezone.utc), "don't \"quote\"",
                           sender=User(id=777, username="bob"))
            return replies()

    ref = scrape._channel_ref("@c")
    msg = _msg(20, datetime(2024, 6, 5, tzinfo=timezone.utc), "body", replies=1)
    row, _ = asyncio.run(scrape._collect_post(ApostropheClient(), ref, msg, _params(tmp_path)))
    assert json.loads(row["Comments List"])[0]["Comment Content"] == "don't \"quote\""


def test_iter_messages_starts_at_date_max(fake_client, tmp_path):
    params = _params(tmp_path)
    scrape.run(Credentials(1, "h", ""), params)
    assert FakeClient.offset_dates == [params.date_max + timedelta(seconds=1)]


def test_no_channel_pause_after_max_messages(fake_client, tmp_path, monkeypatch):
    waits = []

    async def _sleep(delay, *a, **k):
        waits.append(delay)

    monkeypatch.setattr(scrape.asyncio, "sleep", _sleep)
    scrape.run(Credentials(1, "h", ""), _params(tmp_path, channels=["@a", "@b"], max_messages=1,
                                            with_participants=False))
    assert all(w <= 1 for w in waits)


def test_ctrl_c_during_channel_pause_keeps_advanced_cursor(fake_client, tmp_path, monkeypatch):
    async def _sleep(delay, *a, **k):
        if delay > 1:  # the between-channel pause
            raise KeyboardInterrupt

    monkeypatch.setattr(scrape.asyncio, "sleep", _sleep)
    with pytest.raises(SystemExit):
        scrape.run(Credentials(1, "h", ""), _params(tmp_path, channels=["@a", "@b"]))
    meta = json.loads((_ckpt(tmp_path) / "resume.json").read_text())
    assert (meta["channel_index"], meta["last_id"]) == (1, 0)


def test_failed_channel_is_listed_at_the_end(monkeypatch, tmp_path, capsys):
    class BadChannelClient(FakeClient):
        def iter_messages(self, channel, reply_to=None, **k):
            if channel == "@bad" and reply_to is None:
                async def gen():
                    raise RuntimeError("boom")
                    yield
                return gen()
            return super().iter_messages(channel, reply_to=reply_to, **k)

    async def _nosleep(*a, **k):
        return None

    monkeypatch.setattr(scrape, "TelegramClient", BadChannelClient)
    monkeypatch.setattr(scrape.asyncio, "sleep", _nosleep)
    path = scrape.run(Credentials(1, "h", ""), _params(tmp_path, channels=["@bad", "@a"],
                                                   with_participants=False))

    tail = capsys.readouterr().out.split("Concluded")[1]
    assert "1 channel(s) stopped on an error" in tail and "@bad: RuntimeError: boom" in tail
    assert set(pd.read_parquet(path)["Group"]) == {"@a"}  # the run still finished


def test_snapshot_error_after_complete_channel_is_not_listed(fake_client, tmp_path, capsys,
                                                             monkeypatch):
    def failing_snapshot(ckpt_dir, start, dest):
        raise ValueError("snapshot write failed")

    monkeypatch.setattr(scrape, "_write_snapshot", failing_snapshot)
    path = scrape.run(Credentials(1, "h", ""), _params(tmp_path, with_participants=False))

    out = capsys.readouterr().out
    assert "snapshot write failed" in out                         # still logged inline
    assert "stopped on an error" not in out                       # but not called incomplete
    assert list(pd.read_parquet(path)["Message ID"]) == ["30", "20"]


class BadThenDeadClient(FakeClient):
    """`@bad` fails with an ordinary error; channels in `dead` keep losing the connection."""
    dead: set = set()

    def iter_messages(self, channel, reply_to=None, **k):
        if reply_to is None and (channel == "@bad" or channel in self.dead):
            exc = RuntimeError("boom") if channel == "@bad" else ConnectionError("down")

            async def gen():
                raise exc
                yield
            return gen()
        return super().iter_messages(channel, reply_to=reply_to, **k)


def test_failed_channel_is_still_listed_after_resume(monkeypatch, tmp_path, capsys):
    async def _nosleep(*a, **k):
        return None

    monkeypatch.setattr(scrape.asyncio, "sleep", _nosleep)
    monkeypatch.setattr(scrape, "TelegramClient", BadThenDeadClient)
    channels = ["@bad", "@a", "@b"]
    monkeypatch.setattr(BadThenDeadClient, "dead", {"@b"})
    with pytest.raises(SystemExit):
        scrape.run(Credentials(1, "h", ""), _params(tmp_path, channels=channels,
                                                with_participants=False))
    meta = json.loads((_ckpt(tmp_path) / "resume.json").read_text())
    assert meta["failed"] == [["@bad", "RuntimeError: boom"]]

    monkeypatch.setattr(BadThenDeadClient, "dead", set())
    capsys.readouterr()
    scrape.run(Credentials(1, "h", ""), _params(tmp_path, channels=channels, resume=True,
                                            with_participants=False))
    tail = capsys.readouterr().out.split("Concluded")[1]
    assert "1 channel(s) stopped on an error" in tail and "@bad: RuntimeError: boom" in tail


def test_ctrl_c_after_failed_channel_does_not_roll_back_to_it(monkeypatch, tmp_path):
    pauses = []

    async def _sleep(delay, *a, **k):
        if delay > 1:  # the between-channel pause; interrupt the one after @bad
            pauses.append(delay)
            if len(pauses) == 2:
                raise KeyboardInterrupt

    monkeypatch.setattr(scrape.asyncio, "sleep", _sleep)
    monkeypatch.setattr(scrape, "TelegramClient", BadThenDeadClient)
    monkeypatch.setattr(BadThenDeadClient, "dead", set())
    with pytest.raises(SystemExit):
        scrape.run(Credentials(1, "h", ""), _params(tmp_path, channels=["@a", "@bad", "@b"]))
    meta = json.loads((_ckpt(tmp_path) / "resume.json").read_text())
    assert (meta["channel_index"], meta["last_id"]) == (2, 0)
    assert meta["failed"] == [["@bad", "RuntimeError: boom"]]


def test_fresh_run_clears_stale_snapshots(fake_client, tmp_path):
    old = _partial(tmp_path) / "OldChannel_until_00099.parquet"
    old.parent.mkdir(parents=True)
    pd.DataFrame([{**_CK_ROW_30, "Message ID": 777}]).to_parquet(old)

    scrape.run(Credentials(1, "h", ""), _params(tmp_path, with_participants=False))
    assert not old.exists()
    assert list(_partial(tmp_path).glob("SomeChannel_until_*"))  # this run's snapshot stays




def test_comments_list_keeps_non_ascii_text(monkeypatch, tmp_path):
    class CyrillicClient(FakeClient):
        def iter_messages(self, channel, reply_to=None, **k):
            if reply_to is None:
                return super().iter_messages(channel, **k)

            async def replies():
                if reply_to == 20:
                    yield _msg(999, datetime(2024, 6, 5, tzinfo=timezone.utc), "привет",
                               sender=User(id=777, access_hash=1, username="bob"))
            return replies()

    monkeypatch.setattr(scrape, "TelegramClient", CyrillicClient)
    path = scrape.run(Credentials(1, "h", ""), _params(tmp_path, with_participants=False))
    df = pd.read_parquet(path)
    raw = df.loc[df["Message ID"] == "20", "Comments List"].iloc[0]
    assert "привет" in raw and json.loads(raw)[0]["Comment Content"] == "привет"


def test_channel_is_resolved_once(monkeypatch, tmp_path):
    peer = object()  # stands in for the InputPeerChannel of an invite-link chat

    class InviteClient(FakeClient):
        lookups = 0
        iter_args = []

        async def get_input_entity(self, arg):
            if isinstance(arg, str):  # a t.me/+hash lookup is a network call each time
                type(self).lookups += 1
                return peer
            return arg

        def iter_messages(self, channel, **k):
            type(self).iter_args.append(channel)
            return super().iter_messages(channel, **k)

    monkeypatch.setattr(scrape, "TelegramClient", InviteClient)
    scrape.run(Credentials(1, "h", ""), _params(tmp_path, channels=["https://t.me/+AbCdEf"],
                                            with_participants=False))
    assert InviteClient.lookups == 1
    assert len(InviteClient.iter_args) == 2  # the channel + post 20's comment thread
    assert all(a is peer for a in InviteClient.iter_args)


def test_invite_chat_urls_are_message_links(monkeypatch, tmp_path):
    class InviteClient(FakeClient):
        async def get_input_entity(self, arg):
            return InputPeerChannel(1629147115, 1) if isinstance(arg, str) else arg

    monkeypatch.setattr(scrape, "TelegramClient", InviteClient)
    path = scrape.run(Credentials(1, "h", ""), _params(tmp_path, channels=["https://t.me/+AbCdEf"],
                                                   with_participants=False))
    row = pd.read_parquet(path).set_index("Message ID").loc["20"]
    assert row["Group"] == "@+AbCdEf"
    assert row["Url"] == "https://t.me/c/1629147115/20"  # t.me/+hash/20 opens nothing
    assert json.loads(row["Comments List"])[0]["Comment Url"].startswith("https://t.me/c/1629147115/20?comment=")


def test_limit_stop_lists_unfinished_channels(fake_client, tmp_path, capsys):
    scrape.run(Credentials(1, "h", ""), _params(tmp_path, channels=["@a", "@b"], max_messages=1,
                                            with_participants=False))
    tail = capsys.readouterr().out.split("Concluded")[1]
    assert "stopped by max-messages - not scraped or cut short: @a, @b" in tail


def test_full_run_lists_no_unfinished_channels(fake_client, tmp_path, capsys):
    scrape.run(Credentials(1, "h", ""), _params(tmp_path, with_participants=False))
    assert "not scraped or cut short" not in capsys.readouterr().out


def test_persistent_500_escapes_telethon_as_retryable(monkeypatch):
    # a real client: after request_retries Telethon must re-raise the 500 itself,
    # not its generic ValueError, or RETRYABLE_RPC handling never fires
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.tl.functions.help import GetConfigRequest

    async def go():
        client = TelegramClient(StringSession(), 1, "x", **{**scrape.CLIENT_KWARGS, "request_retries": 1})

        def send(request, ordered=False):
            fut = asyncio.get_running_loop().create_future()
            fut.set_exception(RpcCallFailError(request=request))
            return fut

        client._sender.send = send
        await client(GetConfigRequest())

    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: real_sleep(0))
    with pytest.raises(scrape.RETRYABLE_RPC):
        asyncio.run(go())


class DropAfterEveryPostClient(FakeClient):
    """Loses the connection right after each in-window post: progress between every drop."""

    def _main_gen(self, offset_id):
        async def gen():
            for m in _main_messages():
                if offset_id and m.id >= offset_id:
                    continue
                yield m
                if m.id in (30, 20):
                    raise ConnectionError("drop")
        return gen()


def test_connection_retries_reset_after_progress(monkeypatch, tmp_path):
    monkeypatch.setattr(scrape, "TelegramClient", DropAfterEveryPostClient)
    monkeypatch.setattr(scrape, "RESUME_BASE_WAIT", 0)
    monkeypatch.setattr(scrape, "RESUME_MAX_ATTEMPTS", 1)  # two drops, but never two in a row
    path = scrape.run(Credentials(1, "h", ""), _params(tmp_path, with_participants=False))
    assert list(pd.read_parquet(path)["Message ID"]) == ["30", "20"]


def test_failed_channel_gets_its_own_snapshot(monkeypatch, tmp_path):
    class MidFailClient(FakeClient):
        def _main_gen(self, offset_id):
            if type(self).calls[-1][0] != "@a":
                return super()._main_gen(offset_id)
            async def gen():
                for m in _main_messages()[:2]:  # 40 (too new), 30 (saved)
                    yield m
                raise RuntimeError("boom")
            return gen()

    async def _nosleep(*a, **k):
        return None

    monkeypatch.setattr(scrape, "TelegramClient", MidFailClient)
    monkeypatch.setattr(scrape.asyncio, "sleep", _nosleep)
    path = scrape.run(Credentials(1, "h", ""), _params(tmp_path, channels=["@a", "@b"],
                                                   with_participants=False))
    a = pd.read_parquet(next(_partial(tmp_path).glob("a_until_*.parquet")))
    assert a[["Group", "Message ID"]].values.tolist() == [["@a", 30]]
    b = pd.read_parquet(next(_partial(tmp_path).glob("b_until_*.parquet")))
    assert set(b["Group"]) == {"@b"}
    posts = pd.read_parquet(path)
    assert ["@a", "30"] in posts[["Group", "Message ID"]].values.tolist()


# --- service messages / resume hint / account credentials -------------------------

def _service_msg(mid, date):
    from telethon.tl.types import MessageActionPinMessage, MessageService
    return MessageService(id=mid, peer_id=PeerChannel(1), action=MessageActionPinMessage(), date=date)


def test_service_message_not_saved(monkeypatch, tmp_path):
    from scraper.datafiles import read_table

    class WithService(FakeClient):
        def _main_gen(self, offset_id):
            async def gen():
                yield _service_msg(25, datetime(2024, 6, 5, 12, tzinfo=timezone.utc))
                for m in _main_messages():
                    if not (offset_id and m.id >= offset_id):
                        yield m
            return gen()

    monkeypatch.setattr(scrape, "TelegramClient", WithService)
    path = scrape.run(Credentials(1, "h", ""), _params(tmp_path, with_participants=False))

    ids = set(read_table(path)["Message ID"].astype(int))
    assert 25 not in ids and {30, 20} <= ids


def _interrupt_after_service(monkeypatch):
    class Interrupted(FakeClient):
        def _main_gen(self, offset_id):
            async def gen():
                yield _main_messages()[1]  # a real post (id 30): a checkpoint needs data
                yield _service_msg(25, datetime(2024, 6, 5, tzinfo=timezone.utc))
                raise KeyboardInterrupt
            return gen()

    monkeypatch.setattr(scrape, "TelegramClient", Interrupted)


def test_checkpoint_cursor_moves_past_service_message(monkeypatch, tmp_path):
    _interrupt_after_service(monkeypatch)
    with pytest.raises(SystemExit):
        scrape.run(Credentials(1, "h", ""), _params(tmp_path))
    meta = json.loads((_ckpt(tmp_path) / "resume.json").read_text())
    assert meta["last_id"] == 25




def test_account_proxy_and_device_reach_the_client(fake_client, tmp_path):
    creds = Credentials(1, "h", "", proxy=("socks5", "1.2.3.4", 1080), device={"device_model": "Redmi"})
    scrape.run(creds, _params(tmp_path, with_participants=False))
    assert FakeClient.init_kwargs["proxy"] == ("socks5", "1.2.3.4", 1080)
    assert FakeClient.init_kwargs["device_model"] == "Redmi"


# --- groups: the people are the message authors; basic groups have no message links -------

_ANN_HASH = 2**60 + 1  # beyond float64's exact range


def _group_messages():
    ann = User(id=501, access_hash=_ANN_HASH, username="ann", first_name="Ann", last_name=None)
    bob = User(id=777, access_hash=_BOB_HASH, username=None, first_name="Bob", last_name="Ivanov")
    day = datetime(2024, 6, 5, tzinfo=timezone.utc)
    return [
        _msg(30, day, "hi", sender=ann, reacts=False),
        _msg(25, day, "hello", sender=bob, reacts=False),
        _msg(22, day, "again", sender=ann, reacts=False),
        _msg(20, day, "anon admin", sender=Channel(id=888, title="grp", photo=None, date=None),
             reacts=False),
    ]


def test_group_authors_make_the_participants_base(fake_client, tmp_path, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "_main_messages", _group_messages)
    path = scrape.run(Credentials(1, "h", ""), _params(tmp_path, with_comments=False))

    posts = pd.read_parquet(path).set_index("Message ID")
    assert posts.loc["30", "Author Username"] == "ann" and posts.loc["30", "Author Name"] == "Ann"
    assert posts.loc["20", "Author Username"] == "[channel]"

    people = pd.read_parquet(_out(tmp_path, "participants")).set_index("ID")
    assert set(people.index) == {501, 777}  # the anonymous admin (the group itself) is no one
    assert people.loc[501, "Access Hash"] == _ANN_HASH  # exact: not via float64
    assert people.loc[777, "Access Hash"] == _BOB_HASH
    assert (people.loc[501, "Messages"], people.loc[501, "Total"]) == (2, 2)
    assert people.loc[777, "Name"] == "Bob Ivanov"


def test_basic_group_ref_has_no_links():
    ref = scrape._channel_ref("-123456")
    assert ref.slug == "chat123456" and ref.url_base == ""
    assert scrape._url(ref, 5) == "" and scrape._url(ref, 5, 7) == ""
    assert scrape.group_channel("@chat123456") == "-123456"
    # channels keep their t.me/c/ links and round trip
    ref = scrape._channel_ref("-1001629147115")
    assert scrape._url(ref, 5, 7) == "https://t.me/c/1629147115/5?comment=7"
    assert scrape.group_channel("@" + ref.slug) == "-1001629147115"


def test_basic_group_scrape_writes_empty_urls(fake_client, tmp_path):
    path = scrape.run(Credentials(1, "h", ""), _params(tmp_path, channels=["-123456"]))
    df = pd.read_parquet(path)
    assert set(df["Group"]) == {"@chat123456"} and set(df["Url"]) == {""}


# --- forums (topics) and private chats ----------------------------------------------------

def _topic_msg(mid, day, text, topic):
    """A forum message; topic None is the General topic (no forum reply header)."""
    msg = _msg(mid, datetime(2024, 6, day, tzinfo=timezone.utc), text, reacts=False)
    msg.reply_to = (types.SimpleNamespace(forum_topic=True, reply_to_top_id=None, reply_to_msg_id=topic)
                    if topic else None)
    return msg


def _forum_topic(tid, title):
    topic = ForumTopic.__new__(ForumTopic)  # only what _resolve_topic reads
    topic.id, topic.title = tid, title
    return topic


class ForumClient(FakeClient):
    """A forum: General (50), topic 42 (45, 43) and topic 7 (44)."""
    messages = [_topic_msg(50, 9, "general hi", None), _topic_msg(45, 8, "topic hi", 42),
                _topic_msg(44, 7, "other", 7), _topic_msg(43, 6, "topic BYE", 42)]
    topics = {1: "General", 42: "Sales", 7: "Off"}
    iter_calls = []  # (reply_to, search) of each iter_messages

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        type(self).iter_calls = []

    async def get_entity(self, arg):
        return types.SimpleNamespace(title="Forum", forum=True)

    async def get_messages(self, channel, limit=1, offset_date=None, reply_to=None):
        return []

    async def __call__(self, request):
        if type(request).__name__ == "GetForumTopicsByIDRequest":
            return types.SimpleNamespace(topics=[_forum_topic(t, self.topics[t])
                                                 for t in request.topics if t in self.topics])
        return await super().__call__(request)

    def iter_messages(self, channel, search=None, reply_to=None, offset_id=0, offset_date=None):
        type(self).iter_calls.append((reply_to, search))

        async def gen():
            for m in self.messages:
                if reply_to is not None and getattr(m.reply_to, "reply_to_msg_id", None) != reply_to:
                    continue
                if not offset_id or m.id < offset_id:
                    yield m
        return gen()


def _forum_run(monkeypatch, tmp_path, channel, **kw):
    monkeypatch.setattr(scrape, "TelegramClient", ForumClient)
    path = scrape.run(Credentials(1, "h", ""), _params(tmp_path, channels=[channel],
                                                   with_participants=False, **kw))
    return pd.read_parquet(path).set_index("Message ID")


def test_topic_is_parsed_from_links():
    assert scrape._channel_ref("https://t.me/forum/42").topic == 42
    assert scrape._channel_ref("t.me/forum/42/100").topic == 42  # a message inside the topic
    assert scrape._channel_ref("t.me/c/1629147115/42").topic == 42
    for raw in ("@forum", "t.me/forum", "t.me/c/1629147115", "-1001629147115", "https://t.me/+AbCd"):
        assert scrape._channel_ref(raw).topic is None, raw


def test_topic_group_round_trips():
    assert scrape.group_channel("@forum-topic42") == "https://t.me/forum/42"
    assert scrape.group_channel("@c1629147115-topic42") == "https://t.me/c/1629147115/42"
    ref = scrape._channel_ref(scrape.group_channel("@c1629147115-topic42"))
    assert (ref.slug, ref.topic) == ("c1629147115", 42)


def test_forum_posts_carry_their_topic(monkeypatch, tmp_path):
    df = _forum_run(monkeypatch, tmp_path, "@forum")
    assert df["Topic ID"].to_dict() == {"50": 1, "45": 42, "44": 7, "43": 42}
    assert set(df["Group"]) == {"@forum"}
    assert ForumClient.iter_calls == [(None, None)]


def test_topic_link_scrapes_only_that_topic(monkeypatch, tmp_path):
    df = _forum_run(monkeypatch, tmp_path, "https://t.me/forum/42")
    assert sorted(df.index) == ["43", "45"]
    assert set(df["Group"]) == {"@forum-topic42"} and set(df["Topic ID"]) == {42}
    assert df.loc["45", "Url"] == "https://t.me/forum/45"
    assert ForumClient.iter_calls == [(42, None)]  # the topic's thread


def test_topic_keyword_is_matched_locally(monkeypatch, tmp_path):
    # Telethon drops search= next to reply_to=: the keyword must not reach it
    df = _forum_run(monkeypatch, tmp_path, "https://t.me/forum/42", keyword="bye")
    assert list(df.index) == ["43"]
    assert ForumClient.iter_calls == [(42, None)]


def test_general_topic_filters_the_whole_chat(monkeypatch, tmp_path):
    df = _forum_run(monkeypatch, tmp_path, "https://t.me/forum/1")
    assert list(df.index) == ["50"] and set(df["Group"]) == {"@forum-topic1"}
    assert ForumClient.iter_calls == [(None, None)]  # General has no thread to ask for


def test_message_link_in_a_forum_scrapes_the_whole_forum(monkeypatch, tmp_path):
    df = _forum_run(monkeypatch, tmp_path, "https://t.me/forum/45")  # 45 is no topic
    assert len(df) == 4 and set(df["Group"]) == {"@forum"}


def test_post_link_in_a_channel_scrapes_the_whole_channel(fake_client, tmp_path):
    # FakeClient is no forum: the number is a post, no topic is asked for (its __call__ would raise)
    path = scrape.run(Credentials(1, "h", ""), _params(tmp_path, channels=["https://t.me/SomeChannel/20"],
                                                   with_participants=False))
    df = pd.read_parquet(path)
    assert set(df["Group"]) == {"@SomeChannel"} and df["Topic ID"].isna().all()


def test_private_chat_has_no_message_links(monkeypatch, tmp_path):
    class UserClient(FakeClient):
        async def get_input_entity(self, arg):
            return InputPeerUser(777, 1) if isinstance(arg, str) else arg

    monkeypatch.setattr(scrape, "TelegramClient", UserClient)
    path = scrape.run(Credentials(1, "h", ""), _params(tmp_path, channels=["@bob"], with_participants=False))
    df = pd.read_parquet(path)
    assert set(df["Group"]) == {"@bob"} and set(df["Url"]) == {""}


# --- the worker's session; continuing an interrupted scrape -------------------------------

class LoggedOutClient(FakeClient):
    disconnected = False

    async def is_user_authorized(self):
        return False

    async def disconnect(self):
        type(self).disconnected = True


def test_a_logged_out_worker_is_a_clear_error_not_a_phone_prompt(monkeypatch, tmp_path):
    monkeypatch.setattr(scrape, "TelegramClient", LoggedOutClient)
    with pytest.raises(SystemExit, match="no longer authorized"):
        scrape.run(Credentials(1, "h", ""), _params(tmp_path))
    assert LoggedOutClient.disconnected


def test_a_failed_get_me_disconnects_the_client(monkeypatch, tmp_path):
    class NoMe(LoggedOutClient):
        async def is_user_authorized(self):
            return True

        async def get_me(self):
            raise ConnectionError("dropped")

    NoMe.disconnected = False
    monkeypatch.setattr(scrape, "TelegramClient", NoMe)
    with pytest.raises(SystemExit):
        scrape.run(Credentials(1, "h", ""), _params(tmp_path))
    assert NoMe.disconnected


def test_an_interrupted_scrape_is_continued_with_its_own_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(scrape, "TelegramClient", CtrlCClient)
    with pytest.raises(SystemExit):
        scrape.run(Credentials(1, "h", ""), _params(tmp_path, keyword="keep", with_comments=False,
                                                   with_participants=False, max_messages=50))

    meta = scrape.pending_resume(tmp_path, "unit.test")
    assert (meta["with_comments"], meta["with_reactors"], meta["max_messages"]) == (False, False, 50)
    params = scrape.resume_params(meta, tmp_path)
    assert params.resume and params.keyword == "keep" and params.with_participants is False
    assert params.date_min == _params(tmp_path).date_min  # dates survive the round trip

    monkeypatch.setattr(scrape, "TelegramClient", FakeClient)
    path = scrape.run(Credentials(1, "h", ""), params)
    assert "30" in set(pd.read_parquet(path)["Message ID"])  # the post saved before the stop
    assert scrape.pending_resume(tmp_path, "unit.test") is None  # finished: nothing left


def test_resume_params_of_an_old_checkpoint_use_the_defaults(tmp_path):
    meta = {"name": "x", "channels": ["@a"], "date_min": "2024-01-01T00:00:00+00:00",
            "date_max": "2024-01-31T23:59:59+00:00"}
    p = scrape.resume_params(meta, tmp_path)
    assert (p.keyword, p.with_comments, p.with_reactors, p.with_participants) == ("", True, True, True)
    assert p.max_messages == 1_000_000


def test_pending_resume_ignores_a_missing_or_broken_file(tmp_path):
    assert scrape.pending_resume(tmp_path, "x") is None
    ckpt = tmp_path / "x_partial" / "checkpoint"
    ckpt.mkdir(parents=True)
    (ckpt / "resume.json").write_text("{half-written")
    assert scrape.pending_resume(tmp_path, "x") is None


# --- giant runs: the entity cache on disk, a stop from another thread, the account kept ------

def test_the_entity_cache_keeps_entities_but_never_the_login(tmp_path):
    import sqlite3

    from telethon.crypto import AuthKey
    from telethon.sessions import StringSession

    ss = StringSession()
    ss.set_dc(2, "149.154.167.51", 443)
    ss.auth_key = AuthKey(b"k" * 256)

    cache = scrape._EntityCacheSession(str(tmp_path / "entities"))
    cache.set_dc(ss.dc_id, ss.server_address, ss.port)
    cache.auth_key = ss.auth_key
    cache.process_entities(types.SimpleNamespace(users=[
        User(id=501, access_hash=_ANN_HASH, username="ann", first_name="Ann")], chats=[]))
    cache.close()

    db = sqlite3.connect(tmp_path / "entities.session")
    assert db.execute("select auth_key from sessions").fetchone()[0] == b""  # no key on disk
    assert db.execute("select hash, username from entities where id = 501").fetchone() == (_ANN_HASH, "ann")

    reopened = scrape._EntityCacheSession(str(tmp_path / "entities"))  # a resume reuses it
    assert reopened.get_input_entity(501).access_hash == _ANN_HASH


def test_the_cache_goes_with_a_clean_finish_and_stays_for_a_resume(monkeypatch, tmp_path):
    monkeypatch.setattr(scrape, "TelegramClient", CtrlCClient)
    with pytest.raises(SystemExit):
        scrape.run(Credentials(1, "h", ""), _params(tmp_path))
    assert (_ckpt(tmp_path) / "entities.session").exists()  # interrupted: kept for the resume

    monkeypatch.setattr(scrape, "TelegramClient", FakeClient)
    scrape.run(Credentials(1, "h", ""), scrape.resume_params(scrape.pending_resume(tmp_path, "unit.test"),
                                                             tmp_path))
    assert not list(_ckpt(tmp_path).glob("entities.session*"))  # finished: nothing left


class SlowFloodClient(FakeClient):
    """A long flood wait mid-run: the stop must not wait for it to end."""

    def _main_gen(self, offset_id):
        async def gen():
            yield _msg(30, datetime(2024, 6, 6, tzinfo=timezone.utc), "keep")
            await asyncio.sleep(3600)
            yield _msg(20, datetime(2024, 6, 5, tzinfo=timezone.utc), "never")
        return gen()


def test_a_stop_from_another_thread_checkpoints_even_mid_wait(monkeypatch, tmp_path):
    import threading

    monkeypatch.setattr(scrape, "TelegramClient", SlowFloodClient)
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    params = _params(tmp_path, stop=stop, account="personal_sessions/me.jsession")

    with pytest.raises(SystemExit):
        scrape.run(Credentials(1, "h", ""), params)  # returns in ~1 s, not after the hour

    meta = scrape.pending_resume(tmp_path, "unit.test")
    assert meta["last_id"] == 30 and meta["account"] == "personal_sessions/me.jsession"
    assert scrape.resume_params(meta, tmp_path).account == "personal_sessions/me.jsession"


# --- the outputs are written shard by shard: memory doesn't grow with the scrape ------------

def _shard(d, index, rows):
    df = pd.DataFrame(rows)
    if "Author Access Hash" in df.columns:  # as _write_checkpoint stores it: exact Int64
        df["Author Access Hash"] = pd.array([r.get("Author Access Hash") for r in rows], dtype="Int64")
    df.to_parquet(d / f"posts_part_{index:05}.parquet")


def _row(group, mid, date, **kw):
    return {"Type": "text", "Group": group, "Message ID": mid, "Date": date,
            "Comments List": kw.pop("comments", "[]"), "Url": f"u{mid}", **kw}


def test_write_posts_stitches_dedups_and_keeps_metadata(tmp_path, monkeypatch):
    from modules.scraped_files import verify_presets

    d = tmp_path / "ckpt"
    d.mkdir()
    _shard(d, 0, [_row("@a", 30, "2024-06-06 10:00:00", **{"Author Access Hash": _ANN_HASH,
                                                            "Author ID": 501, "Views": None}),
                  _row("@a", 20, "2024-06-05 10:00:00", **{"Author Access Hash": None, "Views": None})])
    # an older shard without the author columns, and a resume overlap re-scraping post 20
    _shard(d, 1, [_row("@a", 20, "2024-06-05 10:00:00"), _row("@a", 20, "2024-06-05 10:00:00"),
                  _row("@b", 99, "2024-06-07 10:00:00", Views=5)])
    reads = []
    real = scrape._post_table
    monkeypatch.setattr(scrape, "_post_table", lambda p: reads.append(p.name) or real(p))

    attrs = {"scrape_window": {"date_min": "2024-06-01", "date_max": "2024-06-30"}, "owner_id": 4242}
    path, rows, span = scrape._write_posts(d, tmp_path, "x", attrs)

    assert reads == ["posts_part_00000.parquet", "posts_part_00001.parquet"]  # one shard at a time
    assert (path.name, rows, span) == ("x_posts_05.06.2024-07.06.2024.parquet", 3, "_05.06.2024-07.06.2024")
    df = pd.read_parquet(path)
    assert list(df["Message ID"]) == ["30", "20", "99"]  # channel by channel, newest first in each
    assert df.attrs == attrs and verify_presets(str(path)) == (["@a", "@b"], ("2024-06-01", "2024-06-30"))
    assert df.loc[0, "Author Access Hash"] == _ANN_HASH  # exact: never via float64
    assert pd.isna(df.loc[1, "Author Access Hash"]) and df.loc[2, "Views"] == 5
    assert list(df["Comments"]) == [0, 0, 0] and str(df["Date"].dtype).startswith("datetime64")
    assert not list(tmp_path.glob("*.tmp"))


def test_write_posts_of_an_empty_scrape_is_an_empty_file(tmp_path):
    d = tmp_path / "ckpt"
    d.mkdir()
    path, rows, span = scrape._write_posts(d, tmp_path, "x", {"owner_id": 1})
    assert (path.name, rows, span) == ("x_posts.parquet", 0, "")
    assert pd.read_parquet(path).empty and pd.read_parquet(path).attrs == {"owner_id": 1}


def test_verify_reads_only_the_ids_of_a_posts_file(tmp_path, monkeypatch):
    from scraper import verify

    path = tmp_path / "x_posts.parquet"
    pd.DataFrame({"Group": ["@a"], "Message ID": ["5"], "Content": ["long text"]}).to_parquet(path)
    seen = []
    real = verify.read_table
    monkeypatch.setattr(verify, "read_table", lambda p, columns=None: seen.append(columns) or real(p, columns))

    df = verify._load_saved(str(path), "@a")
    assert "Content" not in seen[0] and "Content" not in df.columns and list(df["_id"]) == [5]


def test_stop_works_while_connecting(monkeypatch, tmp_path):
    import threading

    class Hanging(FakeClient):
        async def connect(self):
            await asyncio.sleep(60)  # a dead proxy: Telethon retries for hours

    monkeypatch.setattr(scrape, "TelegramClient", Hanging)
    stop = threading.Event()
    threading.Timer(0.1, stop.set).start()
    started = time.monotonic()
    with pytest.raises(SystemExit):
        scrape.run(Credentials(1, "h", ""), _params(tmp_path, stop=stop))
    assert time.monotonic() - started < 10  # not after the connect gave up
