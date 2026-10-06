"""The trigger / messages-count steps of the chat, instant and comments flows (bot/routers/broadcasts.py)."""

import asyncio
import types

from bot.routers import broadcasts
from bot.states import BroadcastOptions, ChatBroadcast, Comments


def ns(**kw):
    return types.SimpleNamespace(**kw)


class _State:
    def __init__(self, data=None):
        self.data = dict(data or {})
        self.state = None

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kw):
        self.data.update(kw)

    async def set_state(self, state):
        self.state = state

    async def clear(self):
        self.data, self.state = {}, None


class _Msg:
    def __init__(self, text=None):
        self.text = text
        self.answers = []
        self.chat = ns(id=1)
        self.bot = object()

    async def answer(self, text, reply_markup=None, **kw):
        self.answers.append((text, reply_markup))


class _Callback:
    def __init__(self):
        self.message = _Msg()

    async def answer(self, *a, **kw):
        pass


class _Settings:
    def __init__(self, trigger="go", messages_count=0):
        self.trigger, self.messages_count = trigger, messages_count
        self.saved = []

    def set_trigger(self, trigger):
        self.saved.append(("trigger", trigger))
        self.trigger = trigger

    def set_messages_count(self, count):
        self.saved.append(("messages_count", count))
        self.messages_count = count


class _Manager:
    def __init__(self, active=False):
        self.active, self.label, self.runs = active, "Другая задача", []

    async def run(self, *args, **kwargs):
        self.runs.append(args)
        return True


def _content(monkeypatch):
    content = ns(cleaned=False)
    content.cleanup = lambda: setattr(content, "cleaned", True)

    async def build(message, album):
        return content
    monkeypatch.setattr(broadcasts, "build_content", build)
    monkeypatch.setattr(broadcasts, "resolve", lambda functions, key: (object(), object()))
    return content


def test_comments_link_asks_the_count_with_the_current_value():
    state, msg = _State(), _Msg("https://t.me/c/1/2")
    asyncio.run(broadcasts.com_link(msg, state, _Settings(messages_count=5)))

    assert state.state == BroadcastOptions.count and state.data["flow"] == "comments"
    text, markup = msg.answers[-1]
    assert "5" in markup.inline_keyboard[0][0].text


def test_count_typed_moves_to_the_flows_message_step():
    state, msg = _State({"flow": "comments"}), _Msg(" 3 ")
    asyncio.run(broadcasts.count_input(msg, state))
    assert state.data["messages_count"] == 3 and state.state == Comments.message


def test_count_garbage_asks_again():
    state, msg = _State({"flow": "comments"}), _Msg("-1")
    asyncio.run(broadcasts.count_input(msg, state))
    assert "messages_count" not in state.data and state.state is None
    assert "0 = без лимита" in msg.answers[-1][0]


def test_count_keep_takes_the_current_setting():
    state, callback = _State({"flow": "chat"}), _Callback()
    asyncio.run(broadcasts.count_keep(callback, state, _Settings(messages_count=7)))
    assert state.data["messages_count"] == 7 and state.state == ChatBroadcast.message


def test_trigger_step_has_no_keep_button_when_empty():
    state, msg = _State(), _Msg()
    asyncio.run(broadcasts._ask_trigger(msg, state, _Settings(trigger="")))
    assert state.state == BroadcastOptions.trigger and msg.answers[-1][1] is None

    asyncio.run(broadcasts._ask_trigger(msg, state, _Settings(trigger="go")))
    assert "go" in msg.answers[-1][1].inline_keyboard[0][0].text


def test_trigger_typed_then_count_asked_for_the_chat():
    state, msg = _State(), _Msg("старт")
    asyncio.run(broadcasts.trigger_input(msg, state, _Settings()))
    assert state.data["trigger"] == "старт" and state.data["flow"] == "chat"
    assert state.state == BroadcastOptions.count


def test_busy_slot_saves_nothing_and_drops_the_content(monkeypatch):
    content = _content(monkeypatch)
    settings, manager = _Settings(), _Manager(active=True)
    state = _State({"link": "https://t.me/c/1/2", "messages_count": 9})
    msg = _Msg()

    asyncio.run(broadcasts.com_run(msg, state, None, None, {}, manager, settings))

    assert settings.saved == [] and content.cleaned and manager.runs == []
    assert "Занят" in msg.answers[-1][0]


def test_start_saves_the_chosen_values(monkeypatch):
    _content(monkeypatch)
    settings, manager = _Settings(trigger="go", messages_count=0), _Manager()
    state = _State({"choice": 0, "trigger": "старт", "messages_count": 4})

    asyncio.run(broadcasts.cha_run(_Msg(), state, None, ns(workers=[]), {}, manager, settings))

    assert settings.saved == [("trigger", "старт"), ("messages_count", 4)] and len(manager.runs) == 1


def test_chat_run_without_a_trigger_is_stale(monkeypatch):
    content = _content(monkeypatch)
    manager, msg = _Manager(), _Msg()
    asyncio.run(broadcasts.cha_run(msg, _State({"choice": 0, "messages_count": 1}), None, ns(workers=[]), {},
                                   manager, _Settings()))
    assert manager.runs == [] and "устарел" in msg.answers[-1][0] and not content.cleaned  # never built
