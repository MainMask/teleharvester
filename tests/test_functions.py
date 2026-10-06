"""Offline tests for the functions/ base layer (no network / telethon calls)."""

import asyncio
import contextlib
import types
from unittest.mock import patch

from functions.base.base import BaseFunction
from functions.base.telethon import TelethonFunction
from functions.changeusername import ChangeUsernameFunc
from telethon.tl.functions.account import CheckUsernameRequest


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
        settings = types.SimpleNamespace(delay=[1], profile_pause=[0])
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


class TestChangeUsernameBaseMode:
    class _Session:
        def __init__(self, taken, error=None):
            self.taken, self.error, self.set_to = taken, error, None

        async def get_me(self):
            return types.SimpleNamespace(first_name="Acc")

        async def __call__(self, request):
            if self.error:
                raise self.error
            if isinstance(request, CheckUsernameRequest):
                return request.username not in self.taken
            self.set_to = request.username  # UpdateUsernameRequest

    def _run(self, sessions, base):
        @contextlib.asynccontextmanager
        async def ainitialize_session(session):
            yield

        storage = types.SimpleNamespace(sessions=list(sessions), ainitialize_session=ainitialize_session,
                                        remember_username=lambda session, username: None)
        fn = ChangeUsernameFunc(storage, types.SimpleNamespace(delay=[1], profile_pause=[0]))
        reports = []

        async def report(msg):
            reports.append(msg)

        asyncio.run(fn.run(report, base=base))
        return reports

    @staticmethod
    def _assert_suffixed(value, base):
        """value is base + a random 2-4 digit suffix (not the bare base, not sequential)."""
        assert value != base and value.startswith(base)
        suffix = value[len(base):]
        assert suffix.isdigit() and 2 <= len(suffix) <= 4

    def test_free_base_is_used_as_is(self):
        s = self._Session(taken=set())
        self._run([s], "CitadelCurator")
        assert s.set_to == "CitadelCurator"

    def test_taken_base_gets_random_suffix(self):
        taken = {"CitadelCurator"}
        s = self._Session(taken=taken)
        self._run([s], "CitadelCurator")
        self._assert_suffixed(s.set_to, "CitadelCurator")
        assert s.set_to not in taken

    def test_accounts_get_distinct_names(self):
        sessions = [self._Session({"CitadelCurator"}) for _ in range(3)]
        self._run(sessions, "CitadelCurator")
        names = [s.set_to for s in sessions]
        assert len(set(names)) == 3  # distinct (the shared `used` set guarantees it)
        for name in names:
            self._assert_suffixed(name, "CitadelCurator")

    def test_leading_at_is_stripped(self):
        s = self._Session(taken=set())
        self._run([s], " @CitadelCurator ")
        assert s.set_to == "CitadelCurator"

    def test_base_on_sale_at_fragment_is_skipped(self):
        from telethon.errors import UsernamePurchaseAvailableError

        class OnSale(self._Session):
            async def __call__(self, request):
                if isinstance(request, CheckUsernameRequest) and request.username == "CitadelCurator":
                    raise UsernamePurchaseAvailableError(request)
                return await super().__call__(request)

        s = OnSale(taken=set())
        self._run([s], "CitadelCurator")
        self._assert_suffixed(s.set_to, "CitadelCurator")  # bare base on sale → a suffixed one

    def test_invalid_candidate_is_skipped(self):
        from telethon.errors import UsernameInvalidError

        class TooShort(self._Session):
            async def __call__(self, request):
                if isinstance(request, CheckUsernameRequest) and request.username == "abcd":
                    raise UsernameInvalidError(request)  # under 5 characters; a suffixed one is fine
                return await super().__call__(request)

        s = TooShort(taken=set())
        self._run([s], "abcd")
        self._assert_suffixed(s.set_to, "abcd")

    def test_check_error_is_reported(self):
        s = self._Session(taken=set(), error=RuntimeError("USERNAME_INVALID"))
        reports = self._run([s], "bad name")
        assert any("USERNAME_INVALID" in m for m in reports)
        assert not any("couldn't find" in m for m in reports)
        assert s.set_to is None


class TestClearPersonalChannel:
    class _Session:
        def __init__(self, error=None):
            self.error, self.requests = error, []

        async def get_me(self):
            return types.SimpleNamespace(first_name="Acc")

        async def __call__(self, request):
            if self.error:
                raise self.error
            self.requests.append(request)

    def _run(self, sessions):
        @contextlib.asynccontextmanager
        async def ainitialize_session(session):
            yield

        storage = types.SimpleNamespace(sessions=list(sessions), ainitialize_session=ainitialize_session)
        from functions.clear_personal_channel import ClearPersonalChannelFunc
        fn = ClearPersonalChannelFunc(storage, types.SimpleNamespace(delay=[1], profile_pause=[0]))
        reports = []

        async def report(msg):
            reports.append(msg)

        asyncio.run(fn.run(report))
        return reports

    def test_clears_every_account(self):
        from telethon.tl.functions.account import UpdatePersonalChannelRequest
        sessions = [self._Session(), self._Session()]
        reports = self._run(sessions)

        assert all(isinstance(s.requests[0], UpdatePersonalChannelRequest) for s in sessions)
        assert sum("cleared" in m for m in reports) == 2

    def test_error_is_reported(self):
        reports = self._run([self._Session(error=RuntimeError("boom"))])
        assert any("not cleared: boom" in m for m in reports)


class TestPollVote:
    class _Session:
        def __init__(self, error=None):
            self.error, self.requests = error, []

        async def get_me(self):
            return types.SimpleNamespace(first_name="Acc")

        async def get_messages(self, channel, ids):
            answer = types.SimpleNamespace(option=b"0")
            return types.SimpleNamespace(poll=types.SimpleNamespace(poll=types.SimpleNamespace(answers=[answer])))

        async def __call__(self, request):
            if self.error:
                raise self.error
            self.requests.append(request)

    def _run(self, sessions):
        @contextlib.asynccontextmanager
        async def ainitialize_session(session):
            yield

        storage = types.SimpleNamespace(sessions=list(sessions), ainitialize_session=ainitialize_session)
        from functions.poll_vote import PollVoteFunc
        fn = PollVoteFunc(storage, types.SimpleNamespace(delay=[0]))
        reports = []

        async def report(msg):
            reports.append(msg)

        asyncio.run(fn.run("https://t.me/chan/5", 0, report))
        return reports

    def test_success_is_reported(self):
        reports = self._run([self._Session(), self._Session()])
        assert sum(m == "[Acc] voted" for m in reports) == 2
        assert "Done: 2/2 accounts" in reports

    def test_error_is_reported(self):
        reports = self._run([self._Session(error=RuntimeError("boom"))])
        assert "[Acc] not voted: boom" in reports
        assert "Done: 0/1 accounts" in reports


class TestHideLastSeen:
    _Session = TestClearPersonalChannel._Session

    def _run(self, sessions):
        @contextlib.asynccontextmanager
        async def ainitialize_session(session):
            yield

        storage = types.SimpleNamespace(sessions=list(sessions), ainitialize_session=ainitialize_session)
        from functions.hide_last_seen import HideLastSeenFunc
        fn = HideLastSeenFunc(storage, types.SimpleNamespace(delay=[1], profile_pause=[0]))
        reports = []

        async def report(msg):
            reports.append(msg)

        asyncio.run(fn.run(report))
        return reports

    def test_hides_on_every_account(self):
        from telethon.tl.functions.account import SetPrivacyRequest
        from telethon.tl.types import InputPrivacyKeyStatusTimestamp, InputPrivacyValueDisallowAll
        sessions = [self._Session(), self._Session()]
        reports = self._run(sessions)

        for s in sessions:
            request = s.requests[0]
            assert isinstance(request, SetPrivacyRequest)
            assert isinstance(request.key, InputPrivacyKeyStatusTimestamp)
            assert [type(rule) for rule in request.rules] == [InputPrivacyValueDisallowAll]
        assert sum("last seen hidden" in m for m in reports) == 2

    def test_error_is_reported(self):
        reports = self._run([self._Session(error=RuntimeError("boom"))])
        assert any("not hidden: boom" in m for m in reports)
