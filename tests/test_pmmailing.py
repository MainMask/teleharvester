"""Offline tests for PmMailingFunc's stateful bits: daily caps, skip-sent, peer resolution.

No disk writes (save_* patched) and no network (fake session). Pins behaviour the
recipient-prep refactor must preserve.
"""

import asyncio
import types
from datetime import date, timedelta
from unittest.mock import patch

from telethon.tl import types as tl_types

from functions.pmmailing import PmMailingFunc


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
