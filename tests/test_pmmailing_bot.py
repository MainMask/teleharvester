"""Offline tests for the PM-mailing recipient step accepting a .txt file or an
inline pasted list (bot/routers/broadcasts.py)."""

import asyncio
import io
import types

from bot.routers import broadcasts


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
    asyncio.run(broadcasts.mail_path(msg, state))
    assert state.data["recipients"] == ["@alice", "+79990001122", "@bob"]
    assert "path" not in state.data


def test_inline_multiline_text_becomes_list():
    msg = _Msg(text="@alice\n@bob\n@carol")
    state = _State()
    asyncio.run(broadcasts.mail_path(msg, state))
    assert state.data["recipients"] == ["@alice", "@bob", "@carol"]


def test_dash_uses_default_targets_path():
    msg = _Msg(text="-")
    state = _State()
    asyncio.run(broadcasts.mail_path(msg, state))
    assert state.data["path"] == broadcasts.DEFAULT_TARGETS
    assert "recipients" not in state.data


def test_single_path_kept_for_backward_compat():
    msg = _Msg(text="assets/targets.txt")
    state = _State()
    asyncio.run(broadcasts.mail_path(msg, state))
    assert state.data["path"] == "assets/targets.txt"
    assert "recipients" not in state.data


def test_parquet_document_is_rejected_with_hint():
    msg = _Msg(document=ns(file_name="db.parquet", file_size=10), file_bytes=b"x")
    state = _State()
    asyncio.run(broadcasts.mail_path(msg, state))
    assert "recipients" not in state.data and "path" not in state.data
    assert any(".parquet" in r for r in msg.replies)


def test_single_username_becomes_one_item_list():
    msg = _Msg(text="@onlyone")
    state = _State()
    asyncio.run(broadcasts.mail_path(msg, state))
    assert state.data["recipients"] == ["@onlyone"]


# --- mail_run: inline recipients bypass load_recipients ----------------

def test_mail_run_uses_inline_recipients(monkeypatch):
    captured = {}

    class _Inst:
        def load_recipients(self, *a):
            raise AssertionError("must not load from file when recipients are inline")

        def load_stats(self):
            pass

        async def run(self, recipients, content, delay, reporter):
            captured["recipients"] = recipients

    class _Manager:
        active = False
        label = ""

        async def run(self, bot, chat_id, pool, instance, bot_function, job, header, done, **kw):
            await job(instance, ns())  # execute the job factory
            return True

    class _Content:
        text = "hi"
        media = None

        def cleanup(self):
            pass

    monkeypatch.setattr(broadcasts, "resolve", lambda functions, key: (_Inst(), ns(risk="risky")))
    monkeypatch.setattr(broadcasts, "build_content", lambda message, album: _fake_content(_Content()))

    msg = _Msg()
    state = _State({"recipients": ["@a", "@b"], "skip": False, "limit": None})

    asyncio.run(broadcasts.mail_run(msg, state, None, ns(), {}, _Manager(), ns(delay=[0])))

    assert captured["recipients"] == ["@a", "@b"]


def test_mail_run_drops_the_stats_ledger_after_the_job(monkeypatch):
    # the ledger of everyone ever messaged is reloaded by every run: the singleton
    # instance must not hold it in memory between mailings
    instance = ns(stats=None)

    async def run(recipients, content, delay, reporter):
        instance.stats = {"1": {"count": 1}}

    instance.run = run

    class _Manager:
        active = False
        label = ""

        async def run(self, bot, chat_id, pool, inst, bot_function, job, header, done, **kw):
            await job(inst, ns())
            return True

    class _Content:
        text = "hi"
        media = None

        def cleanup(self):
            pass

    monkeypatch.setattr(broadcasts, "resolve", lambda functions, key: (instance, ns(risk="risky")))
    monkeypatch.setattr(broadcasts, "build_content", lambda message, album: _fake_content(_Content()))

    state = _State({"recipients": ["@a"], "skip": False, "limit": None})
    asyncio.run(broadcasts.mail_run(_Msg(), state, None, ns(), {}, _Manager(), ns(delay=[0])))

    assert instance.stats == {}


def test_mail_run_drops_the_stats_ledger_when_the_job_never_starts(monkeypatch):
    # "skip already sent" loads the ledger to filter; everyone sent already -> no job runs,
    # and the singleton must not keep the ledger until the next mailing
    instance = ns(stats=None)

    def load_stats():
        instance.stats = {"@a": {"count": 1}}

    instance.load_stats = load_stats
    instance.filter_unsent = lambda recipients: [r for r in recipients if r not in instance.stats]

    monkeypatch.setattr(broadcasts, "resolve", lambda functions, key: (instance, ns(risk="risky")))

    state = _State({"recipients": ["@a"], "skip": True, "limit": None})
    msg = _Msg()
    asyncio.run(broadcasts.mail_run(msg, state, None, ns(), {}, ns(active=False, label=""), ns(delay=[0])))

    assert msg.replies == ["Список получателей пуст."]
    assert instance.stats == {}


async def _fake_content(obj):
    return obj


def test_mail_start_drops_stale_recipients_from_an_abandoned_run():
    class _ClearingState(_State):
        async def clear(self):
            self.data = {}

    state = _ClearingState({"recipients": ["@old"]})
    callback = ns(answer=_noop, message=_Msg())
    pool = ns(count=lambda: 1)

    asyncio.run(broadcasts.mail_start(callback, state, pool))
    asyncio.run(broadcasts.mail_path(_Msg(text="-"), state))

    assert "recipients" not in state.data
    assert state.data["path"] == broadcasts.DEFAULT_TARGETS


async def _noop(*args, **kwargs):
    pass


def test_photo_is_not_taken_as_default_targets():
    """A photo / sticker (no text, no document) must re-prompt, not pick assets/targets.txt."""
    msg = _Msg()
    state = _State()
    asyncio.run(broadcasts.mail_path(msg, state))
    assert "path" not in state.data and "recipients" not in state.data
    assert msg.replies  # re-prompted


# --- mail_run: a stale «Пропускать?» button leaves no recipients behind -------------

def test_mail_run_without_recipients_says_the_flow_is_stale():
    """mail_skip isn't tied to a state: tapped after a /cancel it leads here with neither a path
    nor a list. The operator is told, nothing starts (no KeyError swallowed by aiogram)."""
    runs = []

    class _Manager:
        active, label = False, ""

        async def run(self, *args, **kwargs):
            runs.append(args)

    msg = _Msg()
    asyncio.run(broadcasts.mail_run(msg, _State({"skip": True, "limit": None}), None, ns(), {},
                                    _Manager(), ns(delay=[0])))
    assert msg.replies == ["Флоу устарел, начните заново."] and not runs
