"""Offline tests for PmMailingFunc's stateful bits: daily caps, skip-sent, peer resolution.

No disk writes (save_* patched) and no network (fake session). Pins behaviour the
recipient-prep refactor must preserve.
"""

import asyncio
import json
import types
from contextlib import asynccontextmanager
from datetime import date, timedelta
from unittest.mock import patch

from telethon.tl import types as tl_types

import functions.pmmailing as pm
from functions.pmmailing import PmMailingFunc
from modules.rich_message import RichContent


def _fn():
    storage = types.SimpleNamespace(sessions=[])
    settings = types.SimpleNamespace(delay=[0], per_account_daily=30, account_pause=[0])
    return PmMailingFunc(storage, settings)


class TestDailyCap:
    def test_sent_today_zero_when_no_entry(self):
        fn = _fn()
        fn.limits = {}
        assert fn.account_sent_today("111") == 0

    def test_sent_today_resets_on_new_date(self):
        fn = _fn()
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        fn.limits = {"111": {"date": yesterday, "count": 7}}
        assert fn.account_sent_today("111") == 0

    def test_bump_counts_within_day_and_resets_across_days(self):
        fn = _fn()
        fn.limits = {}
        with patch.object(PmMailingFunc, "save_limits"):
            fn.bump_account("111")
            fn.bump_account("111")
        assert fn.account_sent_today("111") == 2

        # simulate the stored day rolling over
        fn.limits["111"]["date"] = (date.today() - timedelta(days=1)).isoformat()
        with patch.object(PmMailingFunc, "save_limits"):
            fn.bump_account("111")
        assert fn.account_sent_today("111") == 1


class TestFilterUnsent:
    def test_drops_already_recorded(self):
        fn = _fn()
        fn.stats = {"alice": {"count": 1}, "123": {"count": 1}}
        recipients = [
            "alice",                                   # string, already sent
            "bob",                                     # string, new
            {"username": "carol", "user_id": 9},       # dict keyed by username, new
            {"user_id": 123, "access_hash": 5},        # dict keyed by user_id, already sent
        ]
        assert fn.filter_unsent(recipients) == ["bob", {"username": "carol", "user_id": 9}]

    def test_drops_a_base_row_recorded_under_its_username_by_older_stats(self):
        fn = _fn()
        fn.stats = {"alice": {"count": 1}}  # written when a base row's key was its username
        recipients = [
            {"user_id": 1, "access_hash": 5, "username": "alice"},  # already sent
            {"user_id": 2, "access_hash": 6, "username": ""},       # new
        ]
        assert fn.filter_unsent(recipients) == [{"user_id": 2, "access_hash": 6, "username": ""}]


class TestResolvePeer:
    def test_dict_becomes_input_peer_user(self):
        fn = _fn()
        peer = asyncio.run(fn.resolve_peer(None, {"user_id": 42, "access_hash": 7}))
        assert isinstance(peer, tl_types.InputPeerUser)
        assert peer.user_id == 42 and peer.access_hash == 7

    def test_username_passthrough(self):
        fn = _fn()
        assert asyncio.run(fn.resolve_peer(None, "someuser")) == "someuser"

    def test_phone_imports_contact(self):
        fn = _fn()
        imported_user = object()

        async def session(request):  # telethon client is called with the request
            return types.SimpleNamespace(users=[imported_user])

        result = asyncio.run(fn.resolve_peer(session, "+15551234567"))
        assert result is imported_user


class TestStatsSaveBatching:
    """pm_mailing.json is flushed every STATS_SAVE_EVERY successes, not per send,
    and the tail is always persisted by run()."""

    def _run_campaign(self, tmp_path, n_recipients):
        @asynccontextmanager
        async def ctx(session):
            yield

        async def get_me():
            return types.SimpleNamespace(id=1, first_name="A")

        async def anoop(*args, **kwargs):
            return None

        session = types.SimpleNamespace(get_me=get_me, send_message=anoop)
        storage = types.SimpleNamespace(sessions=[session], ainitialize_session=ctx)
        settings = types.SimpleNamespace(delay=[0], per_account_daily=10_000, account_pause=[0])
        fn = PmMailingFunc(storage, settings)

        recipients = [f"user{i}" for i in range(n_recipients)]
        asyncio.run(fn.run(recipients, RichContent(text="hi"), [0], anoop))

    def test_batches_saves_and_flushes_tail(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pm, "STATS_PATH", str(tmp_path / "pm_mailing.json"))
        monkeypatch.setattr(pm, "LIMITS_PATH", str(tmp_path / "account_limits.json"))

        saves = []
        real_save = PmMailingFunc.save_stats

        def spy(self):
            saves.append(1)
            real_save(self)

        with patch.object(PmMailingFunc, "save_stats", spy):
            self._run_campaign(tmp_path, 60)

        # 60 successes, STATS_SAVE_EVERY=25 -> 2 periodic flushes (at 25, 50) + 1 tail flush
        assert pm.STATS_SAVE_EVERY == 25
        assert len(saves) == 3  # not 60 (one per send)

        data = json.loads((tmp_path / "pm_mailing.json").read_text())
        assert len(data) == 60  # tail flushed: every success persisted
