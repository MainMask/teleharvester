"""Offline tests for PmMailingFunc's stateful bits: daily caps, skip-sent, peer resolution.

No disk writes (save_* patched) and no network (fake session). Pins behaviour the
recipient-prep refactor must preserve.
"""

import asyncio
import conftest
import json
import os
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
    storage = types.SimpleNamespace(sessions=[], home=conftest.home())
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
    storage = types.SimpleNamespace(sessions=[session], ainitialize_session=ctx, home=conftest.home())
    settings = types.SimpleNamespace(delay=[0], per_account_daily=cap, account_pause=[0])
    fn = PmMailingFunc(storage, settings)

    recipients = [f"user{i}" for i in range(n_recipients)]
    asyncio.run(fn.run(recipients, RichContent(text="hi"), [0], anoop))
    return list(fn.stats)


class TestDailyCap:
    """per_account_daily goes through modules.account_limits.DailyCounter (tested there)."""

    def test_account_stops_at_its_cap_and_it_is_saved(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pm, "LIMITS_PATH", str(tmp_path / "account_limits.json"))

        assert _run_campaign(3, cap=1) == ["user0"]
        saved = json.loads((tmp_path / "account_limits.json").read_text())
        assert saved == {"1": {"date": date.today().isoformat(), "count": 1}}

    def test_zero_means_unlimited(self, tmp_path, monkeypatch):
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

    def test_a_person_mailed_from_a_list_is_not_mailed_again_from_a_base(self):
        fn = _fn()
        fn.stats = {"@Alice": {"count": 1}}  # a project's uploaded list
        recipients = [
            {"user_id": 1, "access_hash": 5, "username": "alice"},  # the same person, any case
            {"user_id": 2, "access_hash": 6, "username": "bob"},
        ]
        assert fn.filter_unsent(recipients) == [{"user_id": 2, "access_hash": 6, "username": "bob"}]

    def test_a_person_mailed_from_a_base_is_not_mailed_again_from_a_list(self):
        fn = _fn()
        fn._unsaved = 0
        fn.stats = {}
        fn.record_success({"user_id": 1, "access_hash": 5, "username": "Alice"})
        assert fn.stats == {"1": {"count": 1, "last_date": fn.stats["1"]["last_date"], "username": "Alice"}}
        assert fn.filter_unsent(["@alice", "@bob"]) == ["@bob"]

    def test_phones_and_ids_are_not_taken_for_usernames(self):
        fn = _fn()
        fn.stats = {"123": {"count": 1}, "+79991234567": {"count": 1}}
        assert fn.filter_unsent(["123", "+79991234567", "+79990000000"]) == ["+79990000000"]
        assert fn.filter_unsent([{"user_id": 5, "access_hash": 1, "username": ""}]) == \
            [{"user_id": 5, "access_hash": 1, "username": ""}]

    def test_a_list_s_links_are_matched_with_a_base_s_usernames(self, tmp_path):
        # a pasted list and a .txt are read as a project's list is: a t.me link is its @username
        lines = ["https://t.me/alice", "  bobby ", "", "+7 999 123-45-67", "Иван"]
        assert pm.PmMailingFunc.parse_list(lines) == ["@alice", "@bobby", "+7 999 123-45-67", "Иван"]
        path = tmp_path / "list.txt"
        path.write_text("\n".join(lines), encoding="utf-8")
        assert _fn().load_recipients(str(path)) == ["@alice", "@bobby", "+7 999 123-45-67", "Иван"]

        fn = _fn()
        fn._unsaved = 0
        fn.stats = {}
        fn.record_success({"user_id": 1, "access_hash": 5, "username": "alice"})  # mailed from a base
        assert fn.filter_unsent(pm.PmMailingFunc.parse_list(lines)) == ["@bobby", "+7 999 123-45-67", "Иван"]


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
            return types.SimpleNamespace(users=[imported_user], retry_contacts=[])

        result = asyncio.run(fn.resolve_peer(session, "+15551234567"))
        assert result is imported_user


class TestStatsSaveBatching:
    """pm_mailing.json is flushed every STATS_SAVE_EVERY successes, not per send,
    and the tail is always persisted by run()."""

    def test_batches_saves_and_flushes_tail(self, tmp_path, monkeypatch):
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

        data = json.loads(open(os.path.join(conftest.home(), "pm_mailing.json")).read())
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
    storage = types.SimpleNamespace(sessions=list(workers), ainitialize_session=ctx, home=conftest.home(),
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


class TestGoneRecipients:
    """A deleted account or a username nobody has is kept in stats: the next mailing skips it."""

    def test_a_deleted_account_is_kept_and_skipped_next_time(self, tmp_path, monkeypatch):
        _checks, sent, reports, stats = _run_base(
            tmp_path, monkeypatch, _rows(1), lambda ids: [tl_types.RequirementToContactEmpty()],
            refuse=errors.InputUserDeactivatedError(request=None))

        assert sent == [] and stats["1"]["count"] == 0 and stats["1"]["gone"]
        assert f"[A] не отправлено: 1 — {pm.GONE}" in reports
        fn = _fn()
        fn.load_stats()
        assert "gone" in fn.stats["1"]  # saved: the next run() drops them (see the test below)

    def test_a_username_nobody_has_is_kept(self, tmp_path, monkeypatch):
        _checks, _sent, _reports, stats = _run_base(
            tmp_path, monkeypatch, ["@nobody"], refuse=ValueError('No user has "nobody" as username'))
        assert "gone" in stats["@nobody"]

    def test_a_refusal_that_may_pass_later_is_not_kept(self, tmp_path, monkeypatch):
        _checks, _sent, _reports, stats = _run_base(
            tmp_path, monkeypatch, ["bob"], refuse=errors.UserPrivacyRestrictedError(request=None))
        assert stats == {}

    def test_an_invalid_peer_is_not_kept(self, tmp_path, monkeypatch):
        """An older base without Owner ID: its hash may be another account's, the person alive."""
        _checks, _sent, _reports, stats = _run_base(
            tmp_path, monkeypatch, _rows(1), lambda ids: [tl_types.RequirementToContactEmpty()],
            refuse=errors.PeerIdInvalidError(request=None))
        assert stats == {}

    def test_a_worker_out_of_imports_hands_the_number_on(self, tmp_path, monkeypatch):
        """At its import limit a worker's number isn't "not found" (nor gone): the next worker takes it."""
        class _Importing(_Worker):
            def __init__(self, uid, name, limited):
                super().__init__(uid, name)
                self.limited = limited

            async def __call__(self, request):  # contacts.importContacts
                contact = request.contacts[0]
                if self.limited:
                    return types.SimpleNamespace(users=[], retry_contacts=[contact.client_id])
                return types.SimpleNamespace(users=[tl_types.InputPeerUser(7, 70)], retry_contacts=[])

        first, second = _Importing(1, "A", limited=True), _Importing(2, "B", limited=False)
        reports, stats = _run_workers(tmp_path, monkeypatch, ["+79990000000"], [first, second])

        assert first.sent == [] and second.sent == [7]
        assert stats["+79990000000"]["count"] == 1 and "gone" not in stats["+79990000000"]

    def test_an_invalid_peer_falls_back_to_the_username_only_if_it_is_still_theirs(self, tmp_path, monkeypatch):
        class _Renamed(_Worker):
            def __init__(self, owner_now):
                super().__init__(1, "A", answer=lambda ids: [tl_types.RequirementToContactEmpty()] * len(ids))
                self.owner_now = owner_now

            async def get_input_entity(self, username):
                return tl_types.InputPeerUser(self.owner_now, 0)

            async def send_message(self, peer, text, **kwargs):
                if peer.access_hash:  # the base's hash: refused, the username's peer is sent to
                    raise errors.PeerIdInvalidError(request=None)
                self.sent.append(peer.user_id)

        rows = [{"user_id": 1, "access_hash": 11, "username": "alice"}]
        still, taken = _Renamed(owner_now=1), _Renamed(owner_now=99)
        reports, _stats = _run_workers(tmp_path, monkeypatch, rows, [still])
        assert still.sent == [1] and "[A] отправлено (по username): alice, всего: 1" in reports
        reports, stats = _run_workers(tmp_path, monkeypatch, [dict(rows[0], user_id=2)], [taken])
        assert taken.sent == [] and "2" not in stats
        assert "[A] не отправлено: alice @alice теперь у другого аккаунта" in reports

    def test_a_base_row_s_username_nobody_has_is_not_kept(self, tmp_path, monkeypatch):
        """An older base's hash refused, its username gone: the person renamed, not deleted."""
        class _Renamed(_Worker):
            async def get_input_entity(self, username):
                raise errors.UsernameNotOccupiedError(request=None)

        worker = _Renamed(1, "A", answer=lambda ids: [tl_types.RequirementToContactEmpty()] * len(ids),
                          refuse=errors.PeerIdInvalidError(request=None))
        _reports, stats = _run_workers(tmp_path, monkeypatch, [{"user_id": 1, "access_hash": 11,
                                                                 "username": "alice"}], [worker])
        assert stats == {}

    def test_gone_ones_are_not_counted_as_already_sent(self):
        """filter_unsent leaves them to run() (counted there as unreachable); a gone @name taken since is new."""
        fn = _fn()
        fn.stats = {"@bob": {"count": 0, "gone": "2026-01-01 00:00:00"}}
        new_bob = {"user_id": 555, "access_hash": 1, "username": "bob"}
        assert fn.filter_unsent(["@bob", new_bob]) == ["@bob", new_bob]

    def test_one_written_to_before_it_went_is_still_sent(self):
        """@bob mailed, renamed, then his @name gone: his base row (by id, old username) is not mailed again."""
        fn = _fn()
        fn.stats = {"@bob": {"count": 1, "gone": "2026-01-01 00:00:00"}}
        assert fn.filter_unsent([{"user_id": 123, "access_hash": 1, "username": "bob"}]) == []

    def test_a_phone_is_never_kept_as_gone(self, tmp_path, monkeypatch):
        """Telegram gives a number to someone new: a deleted account's phone isn't skipped for good."""
        class _Phone(_Worker):
            async def __call__(self, request):  # contacts.importContacts: found
                return types.SimpleNamespace(users=[tl_types.InputPeerUser(7, 70)], retry_contacts=[])

        worker = _Phone(1, "A", refuse=errors.InputUserDeactivatedError(request=None))
        _reports, stats = _run_workers(tmp_path, monkeypatch, ["+79990000000"], [worker])
        assert stats == {}

    def test_a_deleted_account_s_old_username_is_someone_new_s(self):
        """Gone by id (deleted): it can't rename, so whoever holds its old @name now is someone else."""
        fn = _fn()
        fn.stats = {"5": {"count": 1, "username": "bob", "gone": "2026-01-01 00:00:00"}}
        new_bob = {"user_id": 77, "access_hash": 1, "username": "bob"}
        assert fn.filter_unsent(["@bob", new_bob]) == ["@bob", new_bob]

    def test_a_formatted_phone_is_a_phone_too(self, tmp_path, monkeypatch):
        class _Phone(_Worker):
            async def __call__(self, request):  # contacts.importContacts: found
                return types.SimpleNamespace(users=[tl_types.InputPeerUser(7, 70)], retry_contacts=[])

        worker = _Phone(1, "A", refuse=errors.InputUserDeactivatedError(request=None))
        _reports, stats = _run_workers(tmp_path, monkeypatch, ["+7 999 123-45-67"], [worker])
        assert stats == {}  # as resolve_peer took it: a number, never kept as gone

    def test_drop_gone_leaves_out_the_kept_ones_a_list_s_in_any_case(self):
        fn = _fn()
        fn.stats = {"@Bob": {"count": 0, "gone": "2026-01-01 00:00:00"},
                    "5": {"count": 0, "gone": "2026-01-01 00:00:00"}, "@ann": {"count": 1}}
        rows = [{"user_id": 5, "access_hash": 1, "username": "x"}, {"user_id": 6, "access_hash": 1, "username": "bob"}]

        assert fn.drop_gone(["@Bob", "@bob", "@ann", *rows]) == ["@ann", rows[1]]  # a base row: by its id

    def test_the_stats_table_marks_the_gone_ones(self, monkeypatch):
        from rich.console import Console

        console = Console(width=160)
        monkeypatch.setattr(pm, "console", console)
        fn = _fn()
        fn.stats = {"7": {"count": 2, "last_date": "2026-05-01 10:00:00", "gone": "2026-10-01 10:00:00"}}
        with console.capture() as capture:
            fn.print_stats()
        assert "недоступен с 2026-10-01" in capture.get()  # mailed before, gone since: still marked


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


class TestCachedMe:
    """TelethonFunction.cached_me: the mailing and adding to contacts poll each worker once per run."""

    def test_polled_once_and_a_failure_is_account_limited(self):
        from modules.account_limits import AccountLimited

        calls = []

        async def get_me():
            calls.append(1)
            return types.SimpleNamespace(id=7)

        async def broken():
            raise ConnectionError("proxy down")

        async def dead():
            return None  # Telethon's answer for a banned / logged-out account

        fn = _fn()
        fn._me_cache = {}
        good = types.SimpleNamespace(get_me=get_me)
        assert asyncio.run(fn.cached_me(good)).id == 7
        assert asyncio.run(fn.cached_me(good)).id == 7 and len(calls) == 1
        for session, why in ((types.SimpleNamespace(get_me=broken), "не удалось опросить аккаунт: proxy down"),
                             (types.SimpleNamespace(get_me=dead), "сессия мертва")):
            try:
                asyncio.run(fn.cached_me(session))
            except AccountLimited as err:
                assert why in str(err)
            else:
                raise AssertionError("must raise AccountLimited")
