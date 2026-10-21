"""Offline tests for the PM-mailing recipient step accepting a .txt file or an
inline pasted list (bot/routers/mailing.py)."""

import asyncio
import io
import types

from bot.routers import mailing


def ns(**kw):
    return types.SimpleNamespace(**kw)


class _State:
    def __init__(self, data=None):
        self.data = data or {}
        self.cleared = False

    async def set_state(self, state):
        self.data["_state"] = state

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_data(self):
        return self.data

    async def clear(self):
        self.cleared = True


class _Msg:
    def __init__(self, text=None, document=None, file_bytes=b""):
        self.text = text
        self.document = document
        self._bytes = file_bytes
        self.replies = []
        self.bot = ns(download=self._download)
        self.chat = ns(id=1)

    async def _download(self, document):
        return io.BytesIO(self._bytes)

    async def answer(self, text, **kwargs):
        self.replies.append(text)


# --- mail_path: recipient source ---------------------------------------

def test_txt_document_becomes_recipient_list():
    body = "@alice\n\n+79990001122\n@bob\n"
    msg = _Msg(document=ns(file_name="list.txt", file_size=len(body)), file_bytes=body.encode())
    state = _State()
    asyncio.run(mailing.mail_path(msg, state))
    assert state.data["recipients"] == ["@alice", "+79990001122", "@bob"]
    assert state.data.get("path") is None  # the list replaces a path chosen before


def test_inline_multiline_text_becomes_list():
    msg = _Msg(text="@alice\n@bob\n@carol")
    state = _State()
    asyncio.run(mailing.mail_path(msg, state))
    assert state.data["recipients"] == ["@alice", "@bob", "@carol"]


def test_dash_uses_default_targets_path():
    msg = _Msg(text="-")
    state = _State()
    asyncio.run(mailing.mail_path(msg, state))
    assert state.data["path"] == mailing.DEFAULT_TARGETS
    assert state.data.get("recipients") is None


def test_single_path_kept_for_backward_compat():
    msg = _Msg(text="assets/targets.txt")
    state = _State()
    asyncio.run(mailing.mail_path(msg, state))
    assert state.data["path"] == "assets/targets.txt"
    assert state.data.get("recipients") is None


def test_parquet_document_is_rejected_with_hint():
    msg = _Msg(document=ns(file_name="db.parquet", file_size=10), file_bytes=b"x")
    state = _State()
    asyncio.run(mailing.mail_path(msg, state))
    assert "recipients" not in state.data and "path" not in state.data
    assert any(".parquet" in r for r in msg.replies)


def test_single_username_becomes_one_item_list():
    msg = _Msg(text="@onlyone")
    state = _State()
    asyncio.run(mailing.mail_path(msg, state))
    assert state.data["recipients"] == ["@onlyone"]


# --- mail_run: inline recipients bypass load_recipients ----------------

def test_mail_run_uses_inline_recipients(monkeypatch):
    captured = {}

    class _Inst:
        def load_recipients(self, *a):
            raise AssertionError("must not load from file when recipients are inline")

        def load_stats(self):
            pass

        def drop_gone(self, recipients):
            return recipients

        async def run(self, recipients, content, delay, reporter):
            captured["recipients"] = recipients

    class _Manager:
        active, label, project_id = False, "", 1

        async def run(self, bot, chat_id, pool, instance, bot_function, job, header, done=None, **kw):
            await job(instance, ns())  # execute the job factory
            return True

    class _Content:
        text = "hi"
        media = None

        def cleanup(self):
            pass

    monkeypatch.setattr(mailing, "resolve", lambda functions, key: (_Inst(), ns(risk="risky")))
    monkeypatch.setattr(mailing, "build_content", lambda message, album: _fake_content(_Content()))

    msg = _Msg()
    state = _State({"recipients": ["@a", "@b"], "skip": False, "limit": None})

    asyncio.run(mailing.mail_run(msg, state, None, ns(), {}, _Manager(), ns(delay=[0])))

    assert captured["recipients"] == ["@a", "@b"]


async def _fake_content(obj):
    return obj


def test_mail_run_drops_the_gone_before_the_limit(monkeypatch):
    """The gone take no place in the limit, and none left alive starts no job."""
    from functions.pmmailing import PmMailingFunc

    captured = {}

    class _Inst(PmMailingFunc):
        def __init__(self):
            pass

        def load_stats(self):
            self.stats = {"@a": {"count": 0, "gone": "2026-01-01 00:00:00"}}

        async def run(self, recipients, content, delay, reporter):
            captured["recipients"] = recipients

    class _Manager:
        active, label, project_id = False, "", 1

        async def run(self, bot, chat_id, pool, instance, bot_function, job, header, done=None, **kw):
            await job(instance, ns())
            return True

    monkeypatch.setattr(mailing, "resolve", lambda functions, key: (_Inst(), ns(risk="risky")))
    monkeypatch.setattr(mailing, "build_content", lambda message, album: _fake_content(ns(cleanup=lambda: None)))

    msg = _Msg()
    state = _State({"recipients": ["@a", "@b", "@c"], "skip": False, "limit": 1})
    asyncio.run(mailing.mail_run(msg, state, None, ns(), {}, _Manager(), ns(delay=[0])))
    assert captured["recipients"] == ["@b"]  # @a gone: the limit's place goes to @b
    assert any("Пропущено недоступных" in r and ": 1" in r for r in msg.replies)

    captured.clear()
    msg = _Msg()
    state = _State({"recipients": ["@a"], "skip": False, "limit": None})
    asyncio.run(mailing.mail_run(msg, state, None, ns(), {}, _Manager(), ns(delay=[0])))
    assert captured == {} and any("пуст" in r for r in msg.replies)  # no empty job


def test_mail_start_drops_stale_recipients_from_an_abandoned_run():
    class _ClearingState(_State):
        async def clear(self):
            self.data = {}

    state = _ClearingState({"recipients": ["@old"]})

    asyncio.run(mailing.ask_recipients(_Msg(), state, []))  # a project's card's 📨
    asyncio.run(mailing.mail_path(_Msg(text="-"), state))

    assert state.data.get("recipients") is None
    assert state.data["path"] == mailing.DEFAULT_TARGETS


async def _noop(*args, **kwargs):
    pass


def test_photo_is_not_taken_as_default_targets():
    """A photo / sticker (no text, no document) must re-prompt, not pick assets/targets.txt."""
    msg = _Msg()
    state = _State()
    asyncio.run(mailing.mail_path(msg, state))
    assert "path" not in state.data and "recipients" not in state.data
    assert msg.replies  # re-prompted


# --- mail_run: a stale «Пропускать?» button leaves no recipients behind -------------

def test_mail_run_without_recipients_says_the_flow_is_stale():
    """mail_skip isn't tied to a state: tapped after a /cancel it leads here with neither a path
    nor a list. The operator is told, nothing starts (no KeyError swallowed by aiogram)."""
    runs = []

    class _Manager:
        active, label, project_id = False, "", 1

        async def run(self, *args, **kwargs):
            runs.append(args)

    msg = _Msg()
    asyncio.run(mailing.mail_run(msg, _State({"skip": True, "limit": None}), None, ns(), {},
                                    _Manager(), ns(delay=[0])))
    assert msg.replies == ["Действие устарело — откройте раздел заново."] and not runs
