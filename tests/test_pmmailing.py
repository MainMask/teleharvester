"""Offline tests for PmMailingFunc's stateful bits: daily caps, skip-sent, peer resolution.

No disk writes (save_* patched) and no network (fake session). Pins behaviour the
recipient-prep refactor must preserve.
"""

import asyncio
import json
import types
from contextlib import asynccontextmanager
from datetime import date
from unittest.mock import patch

from telethon import errors
from telethon.tl import types as tl_types

import functions.pmmailing as pm
from functions.pmmailing import PmMailingFunc
from modules.rich_message import RichContent


def _fn():
    storage = types.SimpleNamespace(sessions=[])
    settings = types.SimpleNamespace(delay=[0], per_account_daily=30, account_pause=[0])
    return PmMailingFunc(storage, settings)


def _run_campaign(n_recipients, cap=10_000):
    """One worker mails n recipients; returns the recipients it sent to."""
    @asynccontextmanager
    async def ctx(session):
        yield

    async def get_me():
        return types.SimpleNamespace(id=1, first_name="A")

    async def anoop(*args, **kwargs):
        return None

    session = types.SimpleNamespace(get_me=get_me, send_message=anoop)
    storage = types.SimpleNamespace(sessions=[session], ainitialize_session=ctx)
    settings = types.SimpleNamespace(delay=[0], per_account_daily=cap, account_pause=[0])
    fn = PmMailingFunc(storage, settings)

    recipients = [f"user{i}" for i in range(n_recipients)]
    asyncio.run(fn.run(recipients, RichContent(text="hi"), [0], anoop))
    return list(fn.stats)


class TestDailyCap:
    """per_account_daily goes through modules.account_limits.DailyCounter (tested there)."""

    def test_account_stops_at_its_cap_and_it_is_saved(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pm, "STATS_PATH", str(tmp_path / "pm_mailing.json"))
        monkeypatch.setattr(pm, "LIMITS_PATH", str(tmp_path / "account_limits.json"))

        assert _run_campaign(3, cap=1) == ["user0"]
        saved = json.loads((tmp_path / "account_limits.json").read_text())
        assert saved == {"1": {"date": date.today().isoformat(), "count": 1}}

    def test_zero_means_unlimited(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pm, "STATS_PATH", str(tmp_path / "pm_mailing.json"))
        monkeypatch.setattr(pm, "LIMITS_PATH", str(tmp_path / "account_limits.json"))

        assert len(_run_campaign(3, cap=0)) == 3


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

    def test_batches_saves_and_flushes_tail(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pm, "STATS_PATH", str(tmp_path / "pm_mailing.json"))
        monkeypatch.setattr(pm, "LIMITS_PATH", str(tmp_path / "account_limits.json"))

        saves = []
        real_save = PmMailingFunc.save_stats

        def spy(self):
            saves.append(1)
            real_save(self)

        with patch.object(PmMailingFunc, "save_stats", spy):
            _run_campaign(60)

        # 60 successes, STATS_SAVE_EVERY=25 -> 2 periodic flushes (at 25, 50) + 1 tail flush
        assert pm.STATS_SAVE_EVERY == 25
        assert len(saves) == 3  # not 60 (one per send)

        data = json.loads((tmp_path / "pm_mailing.json").read_text())
        assert len(data) == 60  # tail flushed: every success persisted


def test_recipients_with_rich_markup_are_printed_as_is(monkeypatch):
    # a .txt line is the operator's text: "[/]" in it must not be a MarkupError
    from rich.console import Console

    console = Console(width=120)
    monkeypatch.setattr(pm, "console", console)
    fn = _fn()
    fn.stats = {"[/]user": {"count": 1, "last_date": "2026-01-01 00:00:00"}}
    with console.capture() as capture:
        fn.print_stats()
    assert "[/]user" in capture.get()


class _Worker:
    """A stub worker: answer(ids) is its users.getRequirementsToContact reply, refuse an
    error its sends raise; `sent` collects whom it wrote to (a user id or a username)."""

    def __init__(self, uid, name, premium=False, answer=None, refuse=None):
        self.uid, self.name, self.premium = uid, name, premium
        self.answer, self.refuse = answer, refuse
        self.path, self.checks, self.sent, self.connects = f"{name}.session", [], [], 0

    async def get_me(self):
        return types.SimpleNamespace(id=self.uid, first_name=self.name, premium=self.premium)

    async def send_message(self, peer, text, **kwargs):
        if self.refuse:
            raise self.refuse
        self.sent.append(peer.user_id if isinstance(peer, tl_types.InputPeerUser) else peer)

    async def __call__(self, request):
        ids = [user.user_id for user in request.id]
        self.checks.append(ids)
        return self.answer(ids)


def _run_workers(tmp_path, monkeypatch, recipients, workers, cap=0):
    """The workers mail `recipients`, in this order; returns (reports, stats)."""
    monkeypatch.setattr(pm, "STATS_PATH", str(tmp_path / "pm_mailing.json"))
    monkeypatch.setattr(pm, "LIMITS_PATH", str(tmp_path / "account_limits.json"))
    reports = []

    @asynccontextmanager
    async def ctx(session):
        session.connects += 1
        yield

    async def report(text):
        reports.append(text)

    # worker_id() reads a worker's user id from its .jsession: the base's owner is found by it
    jsessions = {w.path: types.SimpleNamespace(account=types.SimpleNamespace(
        account=types.SimpleNamespace(user_id=w.uid))) for w in workers}
    storage = types.SimpleNamespace(sessions=list(workers), ainitialize_session=ctx,
                                    jsessions_paths=jsessions, get_session_path=lambda s: s.path)
    settings = types.SimpleNamespace(delay=[0], per_account_daily=cap, account_pause=[0])
    fn = PmMailingFunc(storage, settings)
    asyncio.run(fn.run(recipients, RichContent(text="hi"), [0], report))
    return reports, fn.stats


def _run_base(tmp_path, monkeypatch, recipients, answer=None, premium=False, refuse=None):
    """One worker mails `recipients`. Returns (checks, sent, reports, stats)."""
    worker = _Worker(1, "A", premium, answer, refuse)
    reports, stats = _run_workers(tmp_path, monkeypatch, recipients, [worker])
    return worker.checks, worker.sent, reports, stats


def _rows(n):
    return [{"user_id": i, "access_hash": 10 + i} for i in range(1, n + 1)]


class TestContactRequirements:
    """A base row is checked with users.getRequirementsToContact before it is sent to."""

    def test_paid_and_premium_only_are_skipped_unsent(self, tmp_path, monkeypatch):
        answers = {1: tl_types.RequirementToContactEmpty(), 2: tl_types.RequirementToContactPremium(),
                   3: tl_types.RequirementToContactPaidMessages(stars_amount=50)}
        checks, sent, reports, stats = _run_base(
            tmp_path, monkeypatch, _rows(3), lambda ids: [answers[i] for i in ids])

        assert checks == [[1, 2, 3]] and sent == [1] and list(stats) == ["1"]
        assert "[A] пропущено: 2 — пишут только контакты и Premium" in reports
        assert "[A] пропущено: 3 — платные сообщения: 50 ⭐" in reports
        assert "Пропущено по настройкам получателя: только Premium — 1, платные — 1" in reports

    def test_a_premium_worker_writes_to_premium_only(self, tmp_path, monkeypatch):
        _checks, sent, _reports, _stats = _run_base(
            tmp_path, monkeypatch, _rows(1),
            lambda ids: [tl_types.RequirementToContactPremium()], premium=True)
        assert sent == [1]

    def test_checked_in_batches(self, tmp_path, monkeypatch):
        checks, sent, _reports, _stats = _run_base(
            tmp_path, monkeypatch, _rows(150),
            lambda ids: [tl_types.RequirementToContactEmpty()] * len(ids))
        assert [len(batch) for batch in checks] == [100, 50] and len(sent) == 150

    def test_a_failed_check_sends_as_without_it(self, tmp_path, monkeypatch):
        def refuse(ids):
            raise errors.BadRequestError(None, "LIMIT_INVALID")

        checks, sent, reports, _stats = _run_base(tmp_path, monkeypatch, _rows(150), refuse)
        assert len(checks) == 2 and len(sent) == 150
        assert sum("проверка настроек получателей не удалась" in r for r in reports) == 1

    def test_a_txt_list_is_not_checked(self, tmp_path, monkeypatch):
        checks, sent, _reports, _stats = _run_base(tmp_path, monkeypatch, ["bob"])
        assert checks == [] and sent == ["bob"]

    def test_a_refused_send_names_the_reason(self, tmp_path, monkeypatch):
        _checks, sent, reports, stats = _run_base(
            tmp_path, monkeypatch, ["bob"], refuse=errors.ForbiddenError(None, "PRIVACY_PREMIUM_REQUIRED"))
        assert sent == [] and stats == {}
        assert "[A] не отправлено: bob — пишут только контакты и Premium" in reports


def _premium_only_for(*ids):
    """A requirements answer: the given ids are "Premium only", everyone else is open."""
    return lambda asked: [tl_types.RequirementToContactPremium() if i in ids
                          else tl_types.RequirementToContactEmpty() for i in asked]


def _named(n):
    return [{"user_id": i, "access_hash": 10 + i, "username": f"u{i}"} for i in range(1, n + 1)]


class TestPremiumHandoff:
    """A shared-queue recipient refused as "Premium only" goes to a Premium worker of the run."""

    def test_a_premium_worker_takes_them(self, tmp_path, monkeypatch):
        plain = _Worker(1, "A", answer=_premium_only_for(2))
        premium = _Worker(2, "B", premium=True, answer=_premium_only_for(2))
        reports, stats = _run_workers(tmp_path, monkeypatch, _named(3), [plain, premium])

        assert plain.sent == [1, 3] and premium.sent == [2]
        assert set(stats) == {"1", "2", "3"}
        assert "[A] пропущено: u2 — пишут только контакты и Premium, передаю Premium-воркеру" in reports
        assert "Ищу Premium-воркера для 1 получателей «только Premium»" in reports
        assert not any(r.startswith("Пропущено по настройкам") for r in reports)

    def test_the_pass_checks_only_the_handed_over(self, tmp_path, monkeypatch):
        plain = _Worker(1, "A", answer=_premium_only_for(2, 4))
        premium = _Worker(2, "B", premium=True, answer=_premium_only_for(2, 4))
        _run_workers(tmp_path, monkeypatch, _named(6), [plain, premium])

        assert premium.checks == [[2, 4]] and premium.sent == [2, 4]

    def test_without_a_premium_worker_they_stay_unsent(self, tmp_path, monkeypatch):
        plain = _Worker(1, "A", answer=_premium_only_for(2))
        other = _Worker(2, "B", answer=_premium_only_for(2))  # not polled yet: asked in the pass
        reports, stats = _run_workers(tmp_path, monkeypatch, _named(3), [plain, other])

        assert plain.sent == [1, 3] and other.sent == [] and "2" not in stats
        assert "Ищу Premium-воркера для 1 получателей «только Premium»" in reports
        assert "Нет свободного Premium-воркера: 1 получателей «только Premium» не отправлены" in reports
        assert "Пропущено по настройкам получателя: только Premium — 1, платные — 0" in reports
        assert plain.connects == 1 and other.connects == 1  # A, polled without Premium, sits the pass out

    def test_a_lone_worker_without_premium_hands_over_nothing(self, tmp_path, monkeypatch):
        _checks, sent, reports, _stats = _run_base(tmp_path, monkeypatch, _named(1), _premium_only_for(1))
        assert sent == [] and "[A] пропущено: u1 — пишут только контакты и Premium" in reports
        assert not any(r.startswith(("Ищу", "Нет свободного")) for r in reports)

    def test_a_refused_send_is_handed_over_too(self, tmp_path, monkeypatch):
        plain = _Worker(1, "A", refuse=errors.ForbiddenError(None, "PRIVACY_PREMIUM_REQUIRED"))
        premium = _Worker(2, "B", premium=True)
        reports, stats = _run_workers(tmp_path, monkeypatch, ["bob"], [plain, premium])

        assert premium.sent == ["bob"] and list(stats) == ["bob"]
        assert "[A] не отправлено: bob — пишут только контакты и Premium, передаю Premium-воркеру" in reports

    def test_a_premium_worker_out_of_its_cap_takes_none(self, tmp_path, monkeypatch):
        # A hands u1 over, writes to u2 and hits its cap of 1; B writes to u3, hits it on u4
        plain = _Worker(1, "A", answer=_premium_only_for(1))
        premium = _Worker(2, "B", premium=True, answer=_premium_only_for(1))
        reports, _stats = _run_workers(tmp_path, monkeypatch, _named(4), [plain, premium], cap=1)

        assert plain.sent == [2] and premium.sent == [3]
        assert "Нет свободного Premium-воркера: 1 получателей «только Premium» не отправлены" in reports

    def test_the_base_owners_own_people_are_not_handed_over(self, tmp_path, monkeypatch):
        # no username: only the owner's hash reaches them, no other worker may take them
        owner = _Worker(1, "A", answer=_premium_only_for(5))
        premium = _Worker(2, "B", premium=True, answer=_premium_only_for(5))
        rows = [{"user_id": 5, "access_hash": 15, "owner_id": 1}]
        reports, stats = _run_workers(tmp_path, monkeypatch, rows, [owner, premium])

        assert owner.sent == [] and premium.sent == [] and stats == {}
        assert "[A] пропущено: 5 — пишут только контакты и Premium" in reports
