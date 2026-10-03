"""Offline tests for the rate-limit / account-rotation core in functions/base.

These pin the behaviour the deep refactor must preserve: short flood-waits are
waited out, exhausted accounts raise AccountLimited, and run_with_rotation retries
the same item on the next account.
"""

import asyncio
import contextlib
import types
from unittest.mock import patch

from telethon.errors import (
    FloodWaitError,
    PeerFloodError,
    SlowModeWaitError,
    UserDeactivatedBanError,
    UserRestrictedError,
)

from functions.base.base import AccountLimited
from functions.base.telethon import TelethonFunction


def _fn(sessions, delay=(0,)):
    """A TelethonFunction with fake storage/settings; ainitialize_session is a no-op CM."""
    @contextlib.asynccontextmanager
    async def ainitialize_session(session):
        yield session

    storage = types.SimpleNamespace(
        sessions=list(sessions),
        ainitialize_session=ainitialize_session,
    )
    settings = types.SimpleNamespace(delay=list(delay))
    return TelethonFunction(storage, settings)


class TestSafeCall:
    def test_short_wait_is_retried_then_succeeds(self):
        calls = []

        async def make():
            calls.append(1)
            if len(calls) == 1:
                raise FloodWaitError(request=None, capture=5)  # <= rate_wait_limit
            return "ok"

        fn = _fn(["a"])
        with patch("functions.base.base.asyncio.sleep", new=_noop_sleep()):
            result = asyncio.run(fn.safe_call(make))

        assert result == "ok"
        assert len(calls) == 2

    def test_long_wait_raises_account_limited(self):
        async def make():
            raise FloodWaitError(request=None, capture=10_000)  # > rate_wait_limit

        fn = _fn(["a"])
        with patch("functions.base.base.asyncio.sleep", new=_noop_sleep()):
            try:
                asyncio.run(fn.safe_call(make))
                assert False, "expected AccountLimited"
            except AccountLimited:
                pass

    def test_gives_up_after_max_retries(self):
        async def make():
            raise FloodWaitError(request=None, capture=1)  # short, but never-ending

        fn = _fn(["a"])
        with patch("functions.base.base.asyncio.sleep", new=_noop_sleep()):
            try:
                asyncio.run(fn.safe_call(make))
                assert False, "expected AccountLimited after max_rate_retries"
            except AccountLimited:
                pass

    def test_peer_flood_raises_account_limited(self):
        self._assert_limited(PeerFloodError(request=None))

    def test_deactivation_raises_account_limited(self):
        self._assert_limited(UserDeactivatedBanError(request=None))

    def test_restriction_raises_account_limited(self):
        self._assert_limited(UserRestrictedError(request=None))

    def _assert_limited(self, error):
        async def make():
            raise error

        fn = _fn(["a"])
        try:
            asyncio.run(fn.safe_call(make))
            assert False, "expected AccountLimited"
        except AccountLimited:
            pass

    def test_short_slow_mode_is_waited_out(self):
        calls = []

        async def make():
            calls.append(1)
            if len(calls) == 1:
                raise SlowModeWaitError(request=None, capture=5)
            return "ok"

        fn = _fn(["a"])
        with patch("functions.base.base.asyncio.sleep", new=_noop_sleep()):
            assert asyncio.run(fn.safe_call(make)) == "ok"

        assert len(calls) == 2

    def test_long_slow_mode_raises_account_limited(self):
        self._assert_limited(SlowModeWaitError(request=None, capture=10_000))

    def test_import_phone_contact_waits_out_flood(self):
        calls = []

        async def session(request):
            calls.append(1)
            if len(calls) == 1:
                raise FloodWaitError(request=None, capture=5)
            return types.SimpleNamespace(users=["u"])

        fn = _fn([session])
        with patch("functions.base.base.asyncio.sleep", new=_noop_sleep()):
            assert asyncio.run(fn.import_phone_contact(session, "+100")) == ["u"]

        assert len(calls) == 2


class TestRunWithRotation:
    def test_rotates_to_next_account_on_limit(self):
        # first account limits on the 2nd item; the 2nd account picks it up
        attempts = []
        limited_once = {"a"}

        async def action(session, item):
            attempts.append((session, item))
            if session in limited_once and item == 2:
                limited_once.discard(session)  # only limit once, on this item
                raise AccountLimited("cap")

        fn = _fn(["a", "b"])
        with patch("functions.base.base.asyncio.sleep", new=_noop_sleep()):
            asyncio.run(fn.run_with_rotation([1, 2, 3], action))

        # item 2 is retried on "b" after "a" is limited; nothing is skipped
        assert attempts == [("a", 1), ("a", 2), ("b", 2), ("b", 3)]

    def test_stops_when_items_exhausted(self):
        processed = []

        async def action(session, item):
            processed.append(item)

        fn = _fn(["a", "b", "c"])
        with patch("functions.base.base.asyncio.sleep", new=_noop_sleep()):
            asyncio.run(fn.run_with_rotation([1, 2], action))

        assert processed == [1, 2]  # finishes on "a", never touches "b"/"c"

    def test_stops_when_accounts_exhausted(self):
        # every account limits immediately -> item never completes, loop ends cleanly
        async def action(session, item):
            raise AccountLimited("cap")

        fn = _fn(["a", "b"])
        with patch("functions.base.base.asyncio.sleep", new=_noop_sleep()):
            processed = asyncio.run(fn.run_with_rotation([1, 2, 3], action))

        assert processed == 0  # nothing got through; caller can report the untouched remainder

    def test_returns_processed_count_on_partial_exhaustion(self):
        # "a" sends item 1, then both accounts limit on item 2 -> 1 of 3 processed
        async def action(session, item):
            if item == 2:
                raise AccountLimited("cap")

        fn = _fn(["a", "b"])
        with patch("functions.base.base.asyncio.sleep", new=_noop_sleep()):
            processed = asyncio.run(fn.run_with_rotation([1, 2, 3], action))

        assert processed == 1

    def test_returns_full_count_when_all_processed(self):
        async def action(session, item):
            pass

        fn = _fn(["a"])
        with patch("functions.base.base.asyncio.sleep", new=_noop_sleep()):
            processed = asyncio.run(fn.run_with_rotation([1, 2, 3], action))

        assert processed == 3

    def test_delay_runs_between_items_but_not_after_the_last(self):
        sleeps = []

        async def sleep(seconds):
            sleeps.append(seconds)

        async def action(session, item):
            pass

        fn = _fn(["a"], delay=[5])
        with patch("functions.base.base.asyncio.sleep", new=sleep):
            asyncio.run(fn.run_with_rotation([1, 2, 3], action))

        assert sleeps == [5, 5]  # 3 items -> 2 delays, none after the final item


def _noop_sleep():
    async def sleep(_seconds):
        return None

    return sleep
