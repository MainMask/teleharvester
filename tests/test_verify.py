"""Offline tests for `scraper verify` (fake client, no Telegram network)."""

import json
import sys
import time
import types
from datetime import datetime, timezone

import pandas as pd
import pytest
from telethon.errors import ChannelPrivateError, FloodWaitError
from telethon.tl.types import ForumTopic

import scraper.verify as verify
import scraper.scrape as scrape
from scraper.config import Credentials
from scraper.verify import VerifyParams


def _m(mid, y, mo, d, *, service=False, replies=None):
    return types.SimpleNamespace(
        id=mid,
        date=datetime(y, mo, d, 12, 0, tzinfo=timezone.utc),
        action="pinned" if service else None,
        replies=types.SimpleNamespace(replies=replies, comments=True) if replies is not None else None,
    )


# The live channel: id -> message. Ids not here come back as None from get_messages.
CHANNEL = {
    100: _m(100, 2024, 1, 5),
    98:  _m(98, 2024, 1, 4),
    97:  _m(97, 2024, 1, 4, service=True),   # service message (not a post)
    95:  _m(95, 2023, 6, 1),                 # exists but older than date_min
    90:  _m(90, 2024, 1, 2, replies=40),     # a post with a 40-comment thread
    88:  _m(88, 2024, 1, 1),
}
REAL_IN_WINDOW = [88, 90, 98, 100]  # what a complete scrape of 88..100 should hold


class _TotalList(list):
    total = 0


class FakeVerifyClient:
    def __init__(self, *a, **k):
        pass

    async def connect(self):
        pass

    async def is_user_authorized(self):
        return True

    async def disconnect(self):
        return None

    async def get_entity(self, arg):
        return types.SimpleNamespace(title="Fake Ch")

    async def get_input_entity(self, arg):
        return arg

    async def get_messages(self, entity, ids=None, limit=None, reverse=False, offset_date=None, **k):
        if ids is not None:
            return [CHANNEL.get(i) for i in ids]
        ordered = [CHANNEL[k] for k in sorted(CHANNEL, reverse=not reverse)
                   if offset_date is None or CHANNEL[k].date < offset_date]
        out = _TotalList(ordered[:limit] if limit else [])
        out.total = len(CHANNEL)
        return out


class FloodVerifyClient(FakeVerifyClient):
    async def get_messages(self, *a, **k):
        raise FloodWaitError(request=None)


@pytest.fixture(autouse=True)
def _fake(monkeypatch):
    monkeypatch.setattr(scrape, "TelegramClient", FakeVerifyClient)
    monkeypatch.setattr(verify, "BATCH_PAUSE", 0)


def _write_input(tmp_path, ids, **cols):
    df = pd.DataFrame({"Message ID": list(ids), **cols})
    p = tmp_path / "posts.parquet"
    df.to_parquet(p, index=False)
    return str(p)


def _params(tmp_path, ids, **kw):
    return VerifyParams(
        input=_write_input(tmp_path, ids, **kw.pop("cols", {})),
        channel="-100123",
        date_min=datetime(2024, 1, 1, tzinfo=timezone.utc),
        date_max=datetime(2024, 12, 31, 23, 59, 59, tzinfo=timezone.utc),
        **kw,
    )


def test_verify_clean(tmp_path, capsys):
    verify.run(Credentials(1, "h", ""), _params(tmp_path, REAL_IN_WINDOW))
    assert "RESULT: 0 posts missed" in capsys.readouterr().out


def test_verify_detects_missed_post(tmp_path, capsys):
    out = tmp_path / "missed.parquet"
    with pytest.raises(SystemExit):
        verify.run(Credentials(1, "h", ""),
                   _params(tmp_path, [88, 90, 100], output=str(out)))
    log = capsys.readouterr().out
    assert "1 REAL POSTS MISSED" in log and "missed id 98" in log
    flagged = pd.read_parquet(out)
    assert flagged["Message ID"].tolist() == [98]
    assert flagged["Reason"].tolist() == ["missed"]


def test_verify_ignores_service_and_out_of_window(tmp_path, capsys):
    verify.run(Credentials(1, "h", ""), _params(tmp_path, REAL_IN_WINDOW))
    log = capsys.readouterr().out
    assert "1 service" in log and "1 outside dates" in log
    assert "0 REAL POSTS MISSED" in log


def _monotonic_channel(monkeypatch):
    """Ids grow with date in a real channel, which the window bounds rely on; drop 95
    (older than its neighbours), or it would pass for the last message before the window."""
    monkeypatch.delitem(CHANNEL, 95)


def test_verify_detects_short_scrape(tmp_path, capsys, monkeypatch):
    _monotonic_channel(monkeypatch)
    with pytest.raises(SystemExit):
        verify.run(Credentials(1, "h", ""), _params(tmp_path, [90, 98, 100]))
    assert "window starts at id 88" in capsys.readouterr().out


def test_verify_comment_sample(tmp_path, capsys):
    verify.run(Credentials(1, "h", ""),
               _params(tmp_path, REAL_IN_WINDOW,
                       comment_sample=5,
                       cols={"Comments": [0, 10, 0, 0]}))  # post 90 -> only 10 of 40
    log = capsys.readouterr().out
    assert "short thread id 90  captured 10 / server 40" in log
    assert "0 posts missed; 1 thread(s) look short" in log


def test_verify_comment_check_covers_threads_with_nothing_captured(tmp_path, capsys):
    verify.run(Credentials(1, "h", ""),
               _params(tmp_path, REAL_IN_WINDOW,
                       comment_sample=1,
                       cols={"Comments": [0, 0, 0, 0]}))  # post 90's 40 comments all lost
    assert "short thread id 90  captured 0 / server 40" in capsys.readouterr().out


def test_verify_detects_scrape_cut_short_at_the_bottom(tmp_path, capsys, monkeypatch):
    _monotonic_channel(monkeypatch)
    # the channel (from id 88) is older than the window, so its oldest message is out of it
    p = _params(tmp_path, [98, 100], output=str(tmp_path / "missed.parquet"))
    p.date_min = datetime(2024, 1, 2, tzinfo=timezone.utc)
    with pytest.raises(SystemExit):
        verify.run(Credentials(1, "h", ""), p)
    flagged = pd.read_parquet(p.output)
    assert flagged["Message ID"].tolist() == [90]
    assert flagged["Reason"].tolist() == ["before-first-saved"]


def test_verify_detects_scrape_cut_short_at_the_top(tmp_path, capsys):
    # the channel's newest message (id 100, Jan 5) is past the window
    p = _params(tmp_path, [88, 90], output=str(tmp_path / "missed.parquet"))
    p.date_max = datetime(2024, 1, 4, 23, 59, 59, tzinfo=timezone.utc)
    with pytest.raises(SystemExit):
        verify.run(Credentials(1, "h", ""), p)
    flagged = pd.read_parquet(p.output)
    assert flagged["Message ID"].tolist() == [98]
    assert flagged["Reason"].tolist() == ["after-last-saved"]


def test_verify_rejects_reactors_file(tmp_path):
    p = tmp_path / "r.parquet"
    pd.DataFrame({"Message ID": [1], "Reactor ID": [99]}).to_parquet(p, index=False)
    with pytest.raises(SystemExit, match="_reactors"):
        verify.run(Credentials(1, "h", ""), VerifyParams(
            input=str(p), channel="-100123",
            date_min=datetime(2024, 1, 1, tzinfo=timezone.utc),
            date_max=datetime(2024, 12, 31, tzinfo=timezone.utc)))


def test_verify_refuses_a_keyword_scrape(tmp_path):
    # its non-matching posts were left out on purpose: each would come back as "missed"
    p = tmp_path / "kw_posts.parquet"
    df = pd.DataFrame({"Message ID": [88, 100]})
    df.attrs["keyword"] = "крипта"
    df.to_parquet(p, index=False)
    with pytest.raises(SystemExit, match="фильтром по слову «крипта»") as exc:
        verify.run(Credentials(1, "h", ""), VerifyParams(
            input=str(p), channel="-100123",
            date_min=datetime(2024, 1, 1, tzinfo=timezone.utc),
            date_max=datetime(2024, 12, 31, tzinfo=timezone.utc)))
    assert isinstance(exc.value.code, str)  # the bot shows a str code as the reason


def test_verify_folder_skips_reactors_file(tmp_path, capsys):
    # scrape's output folder holds the _reactors file next to the posts
    pd.DataFrame({"Message ID": REAL_IN_WINDOW, "Group": "@a"}).to_parquet(tmp_path / "T_posts.parquet")
    pd.DataFrame({"Group": ["@a"], "Message ID": [90], "Reactor ID": [99]}
                 ).to_parquet(tmp_path / "T_reactors.parquet")
    verify.run(Credentials(1, "h", ""), VerifyParams(
        input=str(tmp_path), channel="@a",
        date_min=datetime(2024, 1, 1, tzinfo=timezone.utc),
        date_max=datetime(2024, 12, 31, 23, 59, 59, tzinfo=timezone.utc)))
    out = capsys.readouterr().out
    assert "skipped T_reactors.parquet: not a posts file" in out
    assert "RESULT: 0 posts missed" in out


def test_verify_dedups_str_and_int_ids(tmp_path, capsys):
    # the _posts file keeps Message ID as str, an _until_ snapshot as int (and no Comments)
    pd.DataFrame({"Message ID": [str(i) for i in REAL_IN_WINDOW], "Group": "@a",
                  "Comments": [0, 10, 0, 0]}).to_parquet(tmp_path / "T_posts.parquet")
    # named to sort before T_posts, so dedup keeps the snapshot's rows
    ten = json.dumps([{"Type": "comment"}] * 10)
    pd.DataFrame({"Message ID": REAL_IN_WINDOW, "Group": "@a",
                  "Comments List": ["[]", ten, "[]", "[]"]}
                 ).to_parquet(tmp_path / "SomeChannel_until_00004.parquet")
    verify.run(Credentials(1, "h", ""), VerifyParams(
        input=str(tmp_path), channel="@a", comment_sample=5,
        date_min=datetime(2024, 1, 1, tzinfo=timezone.utc),
        date_max=datetime(2024, 12, 31, 23, 59, 59, tzinfo=timezone.utc)))
    out = capsys.readouterr().out
    assert "saved posts:          4" in out
    assert "short thread id 90  captured 10 / server 40" in out


def test_verify_folder_with_missed_file_lists_groups(tmp_path):
    # a previous verify's _missed file has no Group column
    pd.DataFrame({"Message ID": [100], "Group": "@a"}).to_parquet(tmp_path / "T_posts.parquet")
    pd.DataFrame({"Message ID": [98], "Reason": ["missed"]}).to_parquet(tmp_path / "T_missed.parquet")
    with pytest.raises(SystemExit, match=r"@zzz.*\['@a'\]"):
        verify.run(Credentials(1, "h", ""), VerifyParams(
            input=str(tmp_path), channel="@zzz",
            date_min=datetime(2024, 1, 1, tzinfo=timezone.utc),
            date_max=datetime(2024, 12, 31, tzinfo=timezone.utc)))


def test_verify_handles_flood(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(scrape, "TelegramClient", FloodVerifyClient)
    with pytest.raises(SystemExit):
        verify.run(Credentials(1, "h", ""), _params(tmp_path, REAL_IN_WINDOW))
    out = capsys.readouterr().out
    assert "Verification interrupted" in out and "FloodWaitError" in out


def test_chunks_streams_a_generator_like_a_list():
    data = list(range(0, 450))
    expected = [data[i:i + 200] for i in range(0, len(data), 200)]
    assert list(verify._chunks(data, 200)) == expected
    assert list(verify._chunks((x for x in data), 200)) == expected  # lazy, not materialised
    assert list(verify._chunks([], 200)) == []


def _multi_channel_params(tmp_path, channel):
    # one scrape of two channels: @a missed its post 98; @b has a post 98 of its own
    return VerifyParams(
        input=_write_input(tmp_path, [100, 90, 88, 98], Group=["@a", "@a", "@a", "@b"]),
        channel=channel,
        date_min=datetime(2024, 1, 1, tzinfo=timezone.utc),
        date_max=datetime(2024, 12, 31, 23, 59, 59, tzinfo=timezone.utc),
    )


def test_verify_uses_only_the_checked_channels_rows(tmp_path, capsys):
    with pytest.raises(SystemExit):
        verify.run(Credentials(1, "h", ""), _multi_channel_params(tmp_path, "https://t.me/A"))
    assert "missed id 98" in capsys.readouterr().out


def test_verify_unknown_channel_lists_groups(tmp_path):
    with pytest.raises(SystemExit, match=r"@zzz.*'@a', '@b'"):
        verify.run(Credentials(1, "h", ""), _multi_channel_params(tmp_path, "@zzz"))


def test_verify_unresolvable_channel_exits_cleanly(tmp_path, monkeypatch):
    class UnknownChannelClient(FakeVerifyClient):
        async def get_entity(self, arg):
            raise ValueError("Cannot find any entity corresponding to x")

    monkeypatch.setattr(scrape, "TelegramClient", UnknownChannelClient)
    with pytest.raises(SystemExit, match="-100123: Cannot find any entity"):
        verify.run(Credentials(1, "h", ""), _params(tmp_path, REAL_IN_WINDOW))


def test_verify_private_channel_exits_cleanly(tmp_path, monkeypatch):
    class PrivateChannelClient(FakeVerifyClient):
        async def get_entity(self, arg):
            raise ChannelPrivateError(request=None)

    monkeypatch.setattr(scrape, "TelegramClient", PrivateChannelClient)
    with pytest.raises(SystemExit, match="-100123: "):
        verify.run(Credentials(1, "h", ""), _params(tmp_path, REAL_IN_WINDOW))


def test_verify_comment_check_ignores_megagroup_reply_threads(tmp_path, capsys, monkeypatch):
    # a group's reply thread (comments=False) is not a comment section; scrape never collects it
    monkeypatch.setitem(CHANNEL, 88, types.SimpleNamespace(
        id=88, date=datetime(2024, 1, 1, 12, tzinfo=timezone.utc), action=None,
        replies=types.SimpleNamespace(replies=40, comments=False)))
    verify.run(Credentials(1, "h", ""),
               _params(tmp_path, REAL_IN_WINDOW, comment_sample=1,
                       cols={"Comments": [0, 40, 0, 0]}))
    assert "short thread" not in capsys.readouterr().out


def test_verify_reports_progress_of_the_absent_id_probe(tmp_path):
    calls = []
    verify.run(Credentials(1, "h", ""), _params(tmp_path, REAL_IN_WINDOW,
                                            on_progress=lambda *a: calls.append(a)))
    n_absent = (max(REAL_IN_WINDOW) - min(REAL_IN_WINDOW) + 1) - len(set(REAL_IN_WINDOW))
    assert calls[0] == (0, n_absent) and calls[-1] == (n_absent, n_absent)


def test_a_stop_ends_the_verify_as_interrupted_without_leaking_the_cancel(tmp_path, monkeypatch):
    import asyncio
    import threading

    async def endless(*a, **k):
        await asyncio.sleep(3600)

    monkeypatch.setattr(verify, "_classify_absent", endless)
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    with pytest.raises(SystemExit) as exc:  # not CancelledError: that would escape the bot's thread
        verify.run(Credentials(1, "h", ""), _params(tmp_path, [88, 90, 100], stop=stop))
    assert exc.value.code == 1


def test_verify_topic_ignores_other_topics(tmp_path, capsys, monkeypatch):
    live = {}
    for mid, topic in ((10, 42), (11, 7), (12, 42), (13, 7)):
        live[mid] = _m(mid, 2024, 1, 5)
        live[mid].reply_to = types.SimpleNamespace(forum_topic=True, reply_to_top_id=None,
                                                   reply_to_msg_id=topic)
    monkeypatch.setattr(sys.modules[__name__], "CHANNEL", live)

    class ForumVerifyClient(FakeVerifyClient):
        async def get_entity(self, arg):
            return types.SimpleNamespace(title="Forum", forum=True)

        async def __call__(self, request):  # GetForumTopicsByIDRequest: topic 42 exists
            topic = ForumTopic.__new__(ForumTopic)
            topic.id, topic.title = 42, "Sales"
            return types.SimpleNamespace(topics=[topic])

    monkeypatch.setattr(scrape, "TelegramClient", ForumVerifyClient)
    params = _params(tmp_path, [10, 12], cols={"Group": ["@forum-topic42"] * 2})
    params.channel = "https://t.me/forum/42"
    verify.run(Credentials(1, "h", ""), params)  # 11 and 13 are another topic's: no SystemExit
    out = capsys.readouterr().out
    assert "1 other topics, 0 REAL POSTS MISSED" in out and "RESULT: 0 posts missed" in out

    params = _params(tmp_path, [10], cols={"Group": ["@forum-topic42"]})
    params.channel = "https://t.me/forum/42"
    with pytest.raises(SystemExit):  # 12 is the topic's own: missed
        verify.run(Credentials(1, "h", ""), params)
    assert "missed" in capsys.readouterr().out


def test_verify_stop_works_while_connecting(tmp_path, capsys, monkeypatch):
    import asyncio
    import threading

    class Hanging(FakeVerifyClient):
        async def connect(self):
            await asyncio.sleep(60)  # a dead proxy: Telethon retries for hours

    monkeypatch.setattr(scrape, "TelegramClient", Hanging)
    stop = threading.Event()
    threading.Timer(0.1, stop.set).start()
    started = time.monotonic()
    with pytest.raises(SystemExit):
        verify.run(Credentials(1, "h", ""), _params(tmp_path, REAL_IN_WINDOW, stop=stop))
    assert time.monotonic() - started < 10  # not after the connect gave up
    assert "Verification interrupted" in capsys.readouterr().out
