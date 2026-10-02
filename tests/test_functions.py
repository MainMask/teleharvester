"""Offline tests for the functions/ base layer (no network / telethon calls)."""

import asyncio
import types
from unittest.mock import patch

from functions.base.base import BaseFunction
from functions.base.telethon import TelethonFunction
from functions.changeusername import ChangeUsernameFunc


def _fn(sessions, delay=(2, 5)):
    """A TelethonFunction wired to fake storage/settings."""
    storage = types.SimpleNamespace(sessions=list(sessions))
    settings = types.SimpleNamespace(delay=list(delay), messages=["m"])
    return TelethonFunction(storage, settings)


class TestAskInt:
    def test_valid(self):
        with patch("functions.base.base.Prompt.ask", side_effect=["3"]):
            assert BaseFunction.ask_int("n", default=1) == 3

    def test_retries_until_valid(self):
        seq = iter(["abc", "-1", "4"])  # junk, below-min, valid
        with patch("functions.base.base.Prompt.ask", side_effect=lambda *a, **k: next(seq)):
            assert BaseFunction.ask_int("n", default=1, min_value=0) == 4

    def test_min_value_enforced(self):
        seq = iter(["0", "0", "1"])
        with patch("functions.base.base.Prompt.ask", side_effect=lambda *a, **k: next(seq)):
            assert BaseFunction.ask_int("n", default=1, min_value=1) == 1


class TestAskAccountsCount:
    def test_empty_storage_does_not_prompt(self):
        fn = _fn([])
        with patch.object(BaseFunction, "ask_int") as ask:
            fn.ask_accounts_count()
        ask.assert_not_called()
        assert fn.sessions == []

    def test_slices_to_choice(self):
        fn = _fn(["a", "b", "c"])
        with patch.object(BaseFunction, "ask_int", return_value=2):
            fn.ask_accounts_count()
        assert fn.sessions == ["a", "b"]

    def test_resets_to_full_list_each_run(self):
        fn = _fn(["a", "b", "c"])
        fn.sessions = ["a"]  # a previous run narrowed it
        with patch.object(BaseFunction, "ask_int", return_value=3):
            fn.ask_accounts_count()
        assert fn.sessions == ["a", "b", "c"]


class TestDelay:
    def _run_delay(self, fn):
        slept = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        with patch("functions.base.base.asyncio.sleep", fake_sleep):
            asyncio.run(fn.delay())
        return slept

    def test_uses_override_over_settings(self):
        fn = _fn(["a"], delay=[2, 5])
        fn.delay_range = [10]
        assert self._run_delay(fn) == [10]

    def test_falls_back_to_settings_when_no_override(self):
        fn = _fn(["a"], delay=[3])
        assert fn.delay_range is None  # class default, not leaked per instance
        assert self._run_delay(fn) == [3]


class TestChangeUsernameFileMode:
    def _run(self, sessions, usernames):
        storage = types.SimpleNamespace(sessions=list(sessions))
        settings = types.SimpleNamespace(delay=[1])
        fn = ChangeUsernameFunc(storage, settings)

        reports, changed = [], []

        async def report(msg):
            reports.append(msg)

        async def fake_change(session, report_, username=None, base=None):
            changed.append(username)

        fn.change = fake_change
        asyncio.run(fn.run(report, usernames=usernames))
        return reports, changed

    def test_warns_and_skips_when_fewer_usernames_than_accounts(self):
        reports, changed = self._run(["s1", "s2", "s3"], ["a", "b"])
        assert any("пропущены" in m for m in reports)
        assert changed == ["a", "b"]  # third account left untouched

    def test_no_warning_when_counts_match(self):
        reports, changed = self._run(["s1", "s2"], ["a", "b"])
        assert not any("пропущены" in m for m in reports)
        assert changed == ["a", "b"]
