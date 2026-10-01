"""Offline test of the scrape loop with a fake Telethon client (no network)."""

import asyncio
import json
import types
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from telethon import utils
from telethon.errors import (
    BroadcastForbiddenError, ChannelPrivateError, FloodWaitError, RpcCallFailError,
)
from telethon.tl.types import Channel, InputPeerChannel, PeerChannel, PeerUser, ReactionEmoji, User

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

    async def start(self, **k):
        return self

    async def disconnect(self):
        return None

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
        fmt=kw.pop("fmt", "parquet"),
        out_dir=tmp_path,
        with_reactors=kw.pop("with_reactors", False),
        **kw,
    )


def test_scrape_end_to_end(fake_client, tmp_path, capsys):
    path = scrape.run(Credentials(1, "hash"), _params(tmp_path))
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


def test_progress_fraction_tracks_message_id_range(fake_client, tmp_path, capsys):
    # _main_messages within [2024-01-01, 2024-12-31]: id_hi=30, id_lo=10, span=20.
    scrape.run(Credentials(1, "h"), _params(tmp_path))
    lines = [ln for ln in capsys.readouterr().out.splitlines() if "% " in ln and "id " in ln]
    assert " 0.0%" in lines[0] and "id 30" in lines[0]      # first kept post: at id_hi
    assert " 50.0%" in lines[1] and "id 20" in lines[1]     # halfway down the id range


def test_scrape_no_comments_flag(fake_client, tmp_path):
    df = pd.read_parquet(scrape.run(Credentials(1, "h"), _params(tmp_path, with_comments=False)))
    assert list(df["Comments List"]) == ["[]", "[]"]


def test_scrape_numeric_channel_id(fake_client, tmp_path, capsys):
    path = scrape.run(Credentials(1, "h"), _params(tmp_path, channels=["-1001629147115"]))
    df = pd.read_parquet(path)
    assert set(df["Group"]) == {"@c1629147115"}
    assert df.iloc[0]["Url"] == "https://t.me/c/1629147115/30"
    assert (tmp_path / "unit.test_partial" / "c1629147115_until_00002.parquet").exists()
    assert '"Fake Channel" (-1001629147115)' in capsys.readouterr().out  # title in the header


def test_scrape_reactors(fake_client, tmp_path):
    scrape.run(Credentials(1, "h"), _params(tmp_path, with_reactors=True))

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
    scrape.run(Credentials(1, "h"),
               _params(tmp_path, with_reactors=True, with_participants=False))

    # posts 30 and 20 report can_see_list=False -> the reactor list is never
    # requested; only the linked thread's comment (can_see_list unset) is fetched
    assert HiddenListClient.reaction_ids == [999]
    r = pd.read_parquet(_out(tmp_path, "reactors"))
    assert set(r["Message ID"]) == {999}


def test_scrape_reactors_excel_format(fake_client, tmp_path):
    scrape.run(Credentials(1, "h"), _params(tmp_path, with_reactors=True, fmt="excel"))
    r = pd.read_excel(_out(tmp_path, "reactors", "xlsx"))
    assert set(r["Message ID"]) == {999} and not r.duplicated().any()


def test_flood_wait_is_waited_out_then_resumes(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(scrape, "TelegramClient", FloodThenOkClient)
    monkeypatch.setattr(scrape, "FLOOD_RETRY_BUFFER", 0)

    path = scrape.run(Credentials(1, "h"),
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
        scrape.run(Credentials(1, "h"), _params(tmp_path, with_reactors=True))

    out = capsys.readouterr().out
    assert "FLOOD_WAIT" in out and "--resume" in out
    assert (_ckpt(tmp_path) / "resume.json").exists()     # checkpoint left for --resume


def test_flood_attempts_reset_after_progress(monkeypatch, tmp_path):
    monkeypatch.setattr(scrape, "TelegramClient", FloodPerPostClient)
    monkeypatch.setattr(scrape, "FLOOD_RETRY_BUFFER", 0)
    monkeypatch.setattr(scrape, "FLOOD_MAX_ATTEMPTS", 1)

    # two bans, but post 30 was saved in between -> not "too many in a row"
    path = scrape.run(Credentials(1, "h"),
                      _params(tmp_path, with_reactors=True, with_participants=False))
    assert list(pd.read_parquet(path)["Message ID"]) == ["30", "20"]


def test_scrape_reactors_without_comments(fake_client, tmp_path):
    # only per-post attempts happen (all 403); must not crash, no reactors file
    scrape.run(Credentials(1, "h"), _params(tmp_path, with_reactors=True, with_comments=False))
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
    scrape.run(Credentials(1, "h"), _params(tmp_path))
    k = FakeClient.init_kwargs
    assert k["connection_retries"] == scrape.CONNECTION_RETRIES
    assert k["retry_delay"] == scrape.RETRY_DELAY
    assert k["request_retries"] == scrape.REQUEST_RETRIES
    assert k["flood_sleep_threshold"] == scrape.FLOOD_SLEEP_THRESHOLD


def test_scrape_default_offset_id_zero(fake_client, tmp_path):
    scrape.run(Credentials(1, "h"), _params(tmp_path))
    assert FakeClient.calls[0][1] == 0


def test_scrape_resumes_after_connection_error(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(scrape, "TelegramClient", FlakyClient)
    monkeypatch.setattr(scrape, "RESUME_BASE_WAIT", 0)
    path = scrape.run(Credentials(1, "h"), _params(tmp_path, with_reactors=True))

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
        scrape.run(Credentials(1, "h"), _params(tmp_path))

    out = capsys.readouterr().out
    assert "scraper scrape" in out and "--resume" in out and "--name unit.test" in out
    meta = json.loads((_ckpt(tmp_path) / "resume.json").read_text())
    assert meta["last_id"] == 30 and meta["channel_index"] == 0
    assert len(pd.read_parquet(_ckpt(tmp_path) / "posts_part_00000.parquet")) == 1


def test_keyboard_interrupt_checkpoints_and_hints(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(scrape, "TelegramClient", CtrlCClient)

    with pytest.raises(SystemExit):
        scrape.run(Credentials(1, "h"), _params(tmp_path))

    out = capsys.readouterr().out
    assert "scraper scrape" in out and "--resume" in out
    meta = json.loads((_ckpt(tmp_path) / "resume.json").read_text())
    assert meta["last_id"] == 30


def test_async_cancel_checkpoints_and_hints(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(scrape, "TelegramClient", CancelClient)

    with pytest.raises(SystemExit):
        scrape.run(Credentials(1, "h"), _params(tmp_path))

    out = capsys.readouterr().out
    assert "scraper scrape" in out and "--resume" in out
    meta = json.loads((_ckpt(tmp_path) / "resume.json").read_text())
    assert meta["last_id"] == 30


def test_resume_command_is_parseable(tmp_path):
    from scraper.cli import build_parser

    p = _params(tmp_path, keyword="war", with_reactors=False, timeout=99)
    argv = scrape._resume_command(p).split()[1:]  # drop the "scraper" prog name
    ns = build_parser().parse_args(argv)
    assert ns.resume is True
    assert ns.name == "unit.test" and ns.keyword == "war" and ns.timeout == 99
    assert ns.no_reactors is True


def test_resume_command_uses_channels_file(tmp_path):
    from scraper.cli import build_parser

    argv = scrape._resume_command(_params(tmp_path, channels_file="chans.txt")).split()[1:]
    assert "--channels-file" in argv and "chans.txt" in argv and "--channels" not in argv
    build_parser().parse_args(argv)  # mutually-exclusive group still satisfied


def test_resume_command_dates_roundtrip(tmp_path):
    p = _params(tmp_path)
    argv = scrape._resume_command(p).split()[1:]
    assert scrape.parse_date(argv[argv.index("--date-min") + 1]) == p.date_min
    assert scrape.parse_date(argv[argv.index("--date-max") + 1], end_of_day=True) == p.date_max


def test_connection_error_during_reactions_triggers_retry(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(scrape, "TelegramClient", ReactorDropClient)
    monkeypatch.setattr(scrape, "RESUME_BASE_WAIT", 0)

    path = scrape.run(Credentials(1, "h"), _params(tmp_path, with_reactors=True))

    assert list(pd.read_parquet(path)["Message ID"]) == ["30", "20"]  # nothing skipped
    assert "retry 1/" in capsys.readouterr().out
    assert len(ReactorDropClient.calls) == 2                          # channel restarted once


def test_server_error_during_reactions_triggers_retry(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(scrape, "TelegramClient", ServerErrorDuringReactionsClient)
    monkeypatch.setattr(scrape, "RESUME_BASE_WAIT", 0)

    path = scrape.run(Credentials(1, "h"), _params(tmp_path, with_reactors=True))

    assert list(pd.read_parquet(path)["Message ID"]) == ["30", "20"]  # thread redone, not truncated
    assert "retry 1/" in capsys.readouterr().out
    assert len(ServerErrorDuringReactionsClient.calls) == 2           # channel restarted once


def test_persistent_server_error_stops_with_resume_hint(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(scrape, "TelegramClient", ServerErrorAfterClient)
    monkeypatch.setattr(scrape, "RESUME_BASE_WAIT", 0)
    monkeypatch.setattr(scrape, "RESUME_MAX_ATTEMPTS", 2)

    with pytest.raises(SystemExit):
        scrape.run(Credentials(1, "h"), _params(tmp_path, with_reactors=True))

    out = capsys.readouterr().out
    assert "scraper scrape" in out and "--resume" in out
    assert (_ckpt(tmp_path) / "resume.json").exists()                 # checkpoint left for --resume


def test_resume_flag_reloads_checkpoint(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(scrape, "TelegramClient", ResumeClient)
    _seed_checkpoint(tmp_path, [_CK_ROW_30, _CK_ROW_20],
                     _resume_meta(tmp_path, last_id=20, t_index=2))

    path = scrape.run(Credentials(1, "h"), _params(tmp_path, resume=True))

    assert "Resuming" in capsys.readouterr().out
    assert ResumeClient.calls[0][1] == 20
    assert list(pd.read_parquet(path)["Message ID"]) == ["30", "20", "15"]


def test_resume_flag_skips_completed_channel(monkeypatch, tmp_path):
    monkeypatch.setattr(scrape, "TelegramClient", FakeClient)
    chans = ["@one", "@two"]
    _seed_checkpoint(tmp_path, [_CK_ROW_30],
                     _resume_meta(tmp_path, channels=chans, channel_index=1, t_index=1))

    scrape.run(Credentials(1, "h"), _params(tmp_path, resume=True, channels=chans))
    assert [c[0] for c in FakeClient.calls] == ["@two"]


def test_resume_flag_param_mismatch(monkeypatch, tmp_path):
    monkeypatch.setattr(scrape, "TelegramClient", FakeClient)
    _seed_checkpoint(tmp_path, [_CK_ROW_30], _resume_meta(tmp_path, channels=["@old"]))

    with pytest.raises(SystemExit, match="does not match"):
        scrape.run(Credentials(1, "h"), _params(tmp_path, resume=True))


def test_resume_flag_missing_json(fake_client, tmp_path, capsys):
    path = scrape.run(Credentials(1, "h"), _params(tmp_path, resume=True))
    assert "not found" in capsys.readouterr().out
    assert list(pd.read_parquet(path)["Message ID"]) == ["30", "20"]


def test_scrape_success_clears_resume_json(fake_client, tmp_path):
    path = scrape.run(Credentials(1, "h"), _params(tmp_path))
    assert path.exists()
    assert not (_ckpt(tmp_path) / "resume.json").exists()


def test_checkpoint_is_parquet_even_for_excel(fake_client, tmp_path):
    # call _scrape directly: run() clears the checkpoint dir on a clean finish
    asyncio.run(scrape._scrape(Credentials(1, "h"),
                               _params(tmp_path, fmt="excel", with_participants=False)))
    d = _ckpt(tmp_path)
    assert list(d.glob("posts_part_*.parquet"))
    assert not list(d.glob("*.xlsx"))
    # the _until_ snapshots too, so `combine --input <name>_partial` can read them
    assert list(d.parent.glob("*_until_*.parquet"))
    assert not list(d.parent.glob("*_until_*.xlsx"))


def test_checkpoint_writes_incremental_shards(fake_client, tmp_path, monkeypatch):
    monkeypatch.setattr(scrape, "CHECKPOINT_EVERY", 1)
    path = scrape.run(Credentials(1, "h"), _params(tmp_path, with_participants=False))
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

    path = scrape.run(Credentials(1, "h"), _params(tmp_path, with_participants=False))
    assert list(pd.read_parquet(path)["Message ID"]) == ["30", "20"]  # 777 not pulled in


def test_clean_finish_removes_checkpoint_dir_contents(fake_client, tmp_path):
    scrape.run(Credentials(1, "h"), _params(tmp_path, with_participants=False))
    d = _ckpt(tmp_path)
    assert not list(d.glob("*.parquet"))
    assert not (d / "resume.json").exists()


def test_resume_flag_missing_checkpoint_refuses(monkeypatch, tmp_path):
    monkeypatch.setattr(scrape, "TelegramClient", FakeClient)
    d = _ckpt(tmp_path)
    d.mkdir(parents=True)
    (d / "resume.json").write_text(json.dumps(_resume_meta(tmp_path, t_index=500, last_id=42)))

    with pytest.raises(SystemExit, match="missing"):
        scrape.run(Credentials(1, "h"), _params(tmp_path, resume=True))


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
        # shard 0: a within-shard repeat + two distinct reactions for msg 1
        pd.DataFrame([_reactor_row(1, 7, "🔥"), _reactor_row(1, 7, "🔥"),
                      _reactor_row(1, 8, "👍")]).to_parquet(d / "reactors_part_00000.parquet")
        # shard 1: msg 1 re-scraped after a --resume (same rows) + a new msg 2
        pd.DataFrame([_reactor_row(1, 7, "🔥"), _reactor_row(1, 8, "👍"),
                      _reactor_row(2, 9, "❤")]).to_parquet(d / "reactors_part_00001.parquet")
        return posts

    monkeypatch.setattr(scrape, "_scrape", fake_scrape)
    scrape.run(Credentials(1, "h"), _params(tmp_path, with_reactors=True, with_participants=False))
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


def test_read_shards_stitches_mismatched_schemas(tmp_path):
    # real scrapes produce shards whose all-None columns (Author/Views/Shares on
    # unsigned or non-broadcast posts) infer a different parquet type per batch
    d = tmp_path / "ckpt"
    d.mkdir()
    pd.DataFrame([{"Message ID": 1, "Author": None, "Views": None},
                  {"Message ID": 2, "Author": None, "Views": None}]
                 ).to_parquet(d / "posts_part_00000.parquet")
    pd.DataFrame([{"Message ID": 3, "Author": "Signed", "Views": 55}]
                 ).to_parquet(d / "posts_part_00001.parquet")

    df = scrape._read_shards(d, "posts")
    assert sorted(df["Message ID"]) == [1, 2, 3]
    assert df.set_index("Message ID").loc[3, "Author"] == "Signed"


def test_eta_uses_current_channel_time(fake_client, tmp_path, monkeypatch, capsys):
    clock = [0.0]

    def _monotonic():
        clock[0] += 20  # every clock read moves time forward
        return clock[0]

    async def _nosleep(*a, **k):
        return None

    monkeypatch.setattr(scrape.time, "monotonic", _monotonic)
    monkeypatch.setattr(scrape.asyncio, "sleep", _nosleep)  # skip the 60s/channel pause
    scrape.run(Credentials(1, "h"), _params(tmp_path, channels=["@a", "@b"],
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
    real = scrape._read_shards

    def spy(ckpt_dir, base, start=0):
        if base == "posts":
            seen_starts.append(start)
        return real(ckpt_dir, base, start)

    monkeypatch.setattr(scrape, "_read_shards", spy)
    scrape.run(Credentials(1, "h"), _params(tmp_path, channels=["@a", "@b"],
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
    scrape.run(Credentials(1, "h"), params)
    assert FakeClient.offset_dates == [params.date_max + timedelta(seconds=1)]


def test_no_channel_pause_after_max_messages(fake_client, tmp_path, monkeypatch):
    waits = []

    async def _sleep(delay, *a, **k):
        waits.append(delay)

    monkeypatch.setattr(scrape.asyncio, "sleep", _sleep)
    scrape.run(Credentials(1, "h"), _params(tmp_path, channels=["@a", "@b"], max_messages=1,
                                            with_participants=False))
    assert all(w <= 1 for w in waits)


def test_ctrl_c_during_channel_pause_keeps_advanced_cursor(fake_client, tmp_path, monkeypatch):
    async def _sleep(delay, *a, **k):
        if delay > 1:  # the between-channel pause
            raise KeyboardInterrupt

    monkeypatch.setattr(scrape.asyncio, "sleep", _sleep)
    with pytest.raises(SystemExit):
        scrape.run(Credentials(1, "h"), _params(tmp_path, channels=["@a", "@b"]))
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
    path = scrape.run(Credentials(1, "h"), _params(tmp_path, channels=["@bad", "@a"],
                                                   with_participants=False))

    tail = capsys.readouterr().out.split("Concluded")[1]
    assert "1 channel(s) stopped on an error" in tail and "@bad: RuntimeError: boom" in tail
    assert set(pd.read_parquet(path)["Group"]) == {"@a"}  # the run still finished


def test_snapshot_error_after_complete_channel_is_not_listed(fake_client, tmp_path, capsys,
                                                             monkeypatch):
    real = scrape.save_table

    def failing_snapshot(df, path, fmt=None):
        if "_until_" in str(path):
            raise ValueError("snapshot write failed")
        return real(df, path, fmt)

    monkeypatch.setattr(scrape, "save_table", failing_snapshot)
    path = scrape.run(Credentials(1, "h"), _params(tmp_path, with_participants=False))

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
        scrape.run(Credentials(1, "h"), _params(tmp_path, channels=channels,
                                                with_participants=False))
    meta = json.loads((_ckpt(tmp_path) / "resume.json").read_text())
    assert meta["failed"] == [["@bad", "RuntimeError: boom"]]

    monkeypatch.setattr(BadThenDeadClient, "dead", set())
    capsys.readouterr()
    scrape.run(Credentials(1, "h"), _params(tmp_path, channels=channels, resume=True,
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
        scrape.run(Credentials(1, "h"), _params(tmp_path, channels=["@a", "@bad", "@b"]))
    meta = json.loads((_ckpt(tmp_path) / "resume.json").read_text())
    assert (meta["channel_index"], meta["last_id"]) == (2, 0)
    assert meta["failed"] == [["@bad", "RuntimeError: boom"]]


def test_fresh_run_clears_stale_snapshots(fake_client, tmp_path):
    old = _partial(tmp_path) / "OldChannel_until_00099.parquet"
    old.parent.mkdir(parents=True)
    pd.DataFrame([{**_CK_ROW_30, "Message ID": 777}]).to_parquet(old)

    scrape.run(Credentials(1, "h"), _params(tmp_path, with_participants=False))
    assert not old.exists()
    assert list(_partial(tmp_path).glob("SomeChannel_until_*"))  # this run's snapshot stays


def test_resume_command_dates_roundtrip_utc_midnight(tmp_path):
    # 03:00+03:00 is exactly 00:00 UTC: an explicit time must not become end-of-day
    p = _params(tmp_path)
    p.date_max = scrape.parse_date("2024-02-01T03:00+03:00", end_of_day=True)
    argv = scrape._resume_command(p).split()[1:]
    assert scrape.parse_date(argv[argv.index("--date-max") + 1], end_of_day=True) == p.date_max


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
    path = scrape.run(Credentials(1, "h"), _params(tmp_path, with_participants=False))
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
    scrape.run(Credentials(1, "h"), _params(tmp_path, channels=["https://t.me/+AbCdEf"],
                                            with_participants=False))
    assert InviteClient.lookups == 1
    assert len(InviteClient.iter_args) == 2  # the channel + post 20's comment thread
    assert all(a is peer for a in InviteClient.iter_args)


def test_invite_chat_urls_are_message_links(monkeypatch, tmp_path):
    class InviteClient(FakeClient):
        async def get_input_entity(self, arg):
            return InputPeerChannel(1629147115, 1) if isinstance(arg, str) else arg

    monkeypatch.setattr(scrape, "TelegramClient", InviteClient)
    path = scrape.run(Credentials(1, "h"), _params(tmp_path, channels=["https://t.me/+AbCdEf"],
                                                   with_participants=False))
    row = pd.read_parquet(path).set_index("Message ID").loc["20"]
    assert row["Group"] == "@+AbCdEf"
    assert row["Url"] == "https://t.me/c/1629147115/20"  # t.me/+hash/20 opens nothing
    assert json.loads(row["Comments List"])[0]["Comment Url"].startswith("https://t.me/c/1629147115/20?comment=")


def test_limit_stop_lists_unfinished_channels(fake_client, tmp_path, capsys):
    scrape.run(Credentials(1, "h"), _params(tmp_path, channels=["@a", "@b"], max_messages=1,
                                            with_participants=False))
    tail = capsys.readouterr().out.split("Concluded")[1]
    assert "stopped by --max-messages/--timeout - not scraped or cut short: @a, @b" in tail


def test_full_run_lists_no_unfinished_channels(fake_client, tmp_path, capsys):
    scrape.run(Credentials(1, "h"), _params(tmp_path, with_participants=False))
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
    path = scrape.run(Credentials(1, "h"), _params(tmp_path, with_participants=False))
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
    path = scrape.run(Credentials(1, "h"), _params(tmp_path, channels=["@a", "@b"],
                                                   with_participants=False))
    a = pd.read_parquet(next(_partial(tmp_path).glob("a_until_*.parquet")))
    assert a[["Group", "Message ID"]].values.tolist() == [["@a", 30]]
    b = pd.read_parquet(next(_partial(tmp_path).glob("b_until_*.parquet")))
    assert set(b["Group"]) == {"@b"}
    posts = pd.read_parquet(path)
    assert ["@a", "30"] in posts[["Group", "Message ID"]].values.tolist()
