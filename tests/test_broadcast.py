"""Offline tests for chat broadcast error handling and PM broadcast phone resolution."""

import asyncio
import contextlib
import types

from telethon.errors import ChatWriteForbiddenError

from functions.broadcast import Broadcast
from functions.pmbroadcast import PmBroadcastFunc
from modules.rich_message import RichContent


def _storage(sessions):
    @contextlib.asynccontextmanager
    async def ainitialize_session(session):
        yield session

    return types.SimpleNamespace(sessions=list(sessions), ainitialize_session=ainitialize_session)


class _Session:
    def __init__(self, name="acc"):
        self.name = name
        self.left = []

    async def get_me(self):
        return types.SimpleNamespace(first_name=self.name)

    async def delete_dialog(self, peer):
        self.left.append(peer)


def _run_broadcast(outcomes, messages_count, delays=None):
    """Run Broadcast.broadcast where each _send yields the next outcome (None = sent);
    delays, if given, gets one entry per delay slept."""
    session = _Session()
    settings = types.SimpleNamespace(delay=[0], messages_count=messages_count, messages=["hi"])
    fn = Broadcast(_storage([session]), settings)
    fn.configure(choice=0, content=RichContent(text="hi"))

    outcomes = iter(outcomes)
    sends = []

    async def _send(*args, **kwargs):
        sends.append(1)
        err = next(outcomes)
        if err is not None:
            raise err

    async def _no_delay():
        if delays is not None:
            delays.append(1)

    fn._send = _send
    fn.delay = _no_delay

    reports = []

    async def report(text):
        reports.append(text)

    asyncio.run(fn.broadcast(session, "chat", report))
    return session, sends, reports


class TestBroadcastErrors:
    def test_three_errors_in_a_row_stop_without_leaving(self):
        session, sends, reports = _run_broadcast([ValueError("bad")] * 3 + [None] * 5, 0)

        assert len(sends) == 3
        assert session.left == []
        assert any("3 errors in a row" in r for r in reports)

    def test_success_resets_error_counter(self):
        e = ValueError("bad")
        session, sends, _ = _run_broadcast([e, e, None, e, e, None], 2)

        assert len(sends) == 6  # never 3 errors in a row -> reached messages_count
        assert session.left == []

    def test_chat_error_leaves_immediately(self):
        session, sends, _ = _run_broadcast([ChatWriteForbiddenError(request=None), None], 0)

        assert len(sends) == 1
        assert session.left == ["chat"]


class TestDelayBetweenSendsOnly:
    def test_no_delay_after_the_last_message(self):
        delays = []
        _, sends, _ = _run_broadcast([None, None], 2, delays)

        assert len(sends) == 2 and len(delays) == 1  # between the two sends, not after

    def test_errors_still_wait_in_an_unlimited_campaign(self):
        delays = []
        _run_broadcast([ValueError("bad")] * 3, 0, delays)

        assert len(delays) == 2  # no tight error loop; the third error stops it

    def test_comments_no_delay_after_the_last_message(self, monkeypatch):
        from functions import broadcast_comments
        from functions.broadcast_comments import CommentsBroadcastFunc

        sends, delays = [], []

        async def send(*args, **kwargs):
            sends.append(1)

        async def delay():
            delays.append(1)

        monkeypatch.setattr(broadcast_comments.rich_message, "send", send)
        fn = CommentsBroadcastFunc(_storage([_Session()]), types.SimpleNamespace(delay=[0], messages_count=2))
        fn.delay = delay

        async def report(text):
            pass

        asyncio.run(fn.broadcast(_Session(), "chan", 5, RichContent(text="hi"), report))

        assert len(sends) == 2 and len(delays) == 1


class TestPmBroadcastPhone:
    def test_resolve_failure_on_one_account_does_not_abort_others(self, monkeypatch):
        bad, good = _Session("bad"), _Session("good")
        fn = PmBroadcastFunc(_storage([bad, good]), types.SimpleNamespace(delay=[0]))

        async def import_phone_contact(session, phone):
            if session is bad:
                raise RuntimeError("resolve failed")
            return [types.SimpleNamespace(id=1)]

        fn.import_phone_contact = import_phone_contact

        sent = []

        async def fake_send(session, peer, content, safe_call, report=None, **kwargs):
            sent.append(session.name)

        monkeypatch.setattr("functions.pmbroadcast.rich_message.send", fake_send)

        reports = []

        async def report(text):
            reports.append(text)

        asyncio.run(fn.run("+100", RichContent(text="hi"), True, report))

        assert sent == ["good"]
        assert any("couldn't resolve phone" in r and "resolve failed" in r for r in reports)

    def test_get_me_failure_is_reported(self):
        class _Broken(_Session):
            async def get_me(self):
                raise RuntimeError("auth key")

        fn = PmBroadcastFunc(_storage([_Broken()]), types.SimpleNamespace(delay=[0]))

        reports = []

        async def report(text):
            reports.append(text)

        asyncio.run(fn.run("user", RichContent(text="hi"), False, report))

        assert any("get_me failed" in r and "auth key" in r for r in reports)
