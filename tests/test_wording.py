"""Questions that say what really happens: the report takes the post from its link, the mention
buttons name as many people as the broadcast really mentions."""

import asyncio
import types

import pytest

from bot.keyboards.common import cancel_kb
from bot.states import ReportMessage
from functions.report import ReportFunc
from test_sync import _Msg, _SCallback, _State, _buttons


@pytest.mark.parametrize("link, post", [
    ("https://t.me/name/123", 123),
    ("t.me/c/1234567/89", 89),                 # a private chat's post
    ("https://t.me/name/42/123", 123),         # a forum topic's post
    ("https://t.me/name/123?comment=5", 123),
    ("@name", None), ("https://t.me/name", None), ("t.me/c/1234567", None),  # the chat itself
    ("https://t.me/+abcDEF", None), ("-1001629147115", None),
])
def test_post_id_is_the_post_a_link_points_at(link, post):
    assert ReportFunc.post_id(link) == post


def test_bot_report_offers_the_post_of_its_link():
    from bot.routers import moderation
    from bot.states import ReportMessage

    state = _State()
    msg = _Msg(text="https://t.me/name/123")
    asyncio.run(moderation.rm_link(msg, state))
    text, markup = msg.answers[0]
    assert text.startswith("В ссылке пост 123") and _buttons(markup) == ["✅ Только на пост 123", "✖️ Отмена"]
    assert state.state == ReportMessage.ids

    go = _SCallback()
    asyncio.run(moderation.rm_post(go, state))
    assert state.data["ids"] == [123] and state.state == ReportMessage.comment
    assert go.message.answers[0][0] == moderation.COMMENT_ASK


def test_bot_report_asks_the_numbers_for_a_chat_link_and_takes_them_typed():
    from bot.routers import moderation

    state = _State()
    msg = _Msg(text="https://t.me/name")
    asyncio.run(moderation.rm_link(msg, state))
    assert msg.answers[0] == (moderation.POSTS_ASK, cancel_kb(ReportMessage.ids))

    typed = _Msg(text="12, 13")
    asyncio.run(moderation.rm_ids(typed, state))
    assert state.data["ids"] == [12, 13] and typed.answers[0][0] == moderation.COMMENT_ASK


def test_cli_report_takes_the_post_from_its_link(monkeypatch):
    from functions import report

    class Stop(Exception):
        pass

    asked, answers = [], iter(["https://t.me/name/123", ""])  # the link, then Enter: the default

    def ask(prompt, default=None, **kwargs):
        asked.append(default)
        return next(answers) or default

    def stop(*args, **kwargs):  # the comment: nothing past it is run here
        raise Stop

    monkeypatch.setattr(report.Prompt, "ask", ask)
    monkeypatch.setattr(report.console, "input", stop)
    func = report.ReportFunc(types.SimpleNamespace(sessions=[object()]), types.SimpleNamespace(delay=[0]))
    monkeypatch.setattr(func, "ask_int", lambda *args, **kwargs: 1)
    with pytest.raises(Stop):
        asyncio.run(func.execute())
    assert asked == [None, "123"]


def test_cli_report_asks_again_when_a_link_has_no_post_and_enter_is_pressed(monkeypatch):
    from functions import report

    class Stop(Exception):
        pass

    answers = iter(["@chan", "", "5"])  # a channel's link: no post to default to; Enter; then a number

    def ask(prompt, default=None, **kwargs):  # as rich: Enter gives the default, None here
        return next(answers) or default

    def stop(*args, **kwargs):
        raise Stop

    monkeypatch.setattr(report.Prompt, "ask", ask)
    monkeypatch.setattr(report.console, "input", stop)  # reached only once the posts are taken
    func = report.ReportFunc(types.SimpleNamespace(sessions=[object()]), types.SimpleNamespace(delay=[0]))
    monkeypatch.setattr(func, "ask_int", lambda *args, **kwargs: 1)
    with pytest.raises(Stop):
        asyncio.run(func.execute())


def test_the_mention_buttons_name_as_many_as_are_mentioned():
    from bot.routers.broadcasts import MMODE_OPTIONS
    from functions.broadcast import MENTION_ADMINS, MENTION_USERS

    assert [label for label, _ in MMODE_OPTIONS] == [f"👮 {MENTION_ADMINS} случайных админа",
                                                     f"👤 {MENTION_USERS} случайных участников"]


@pytest.mark.parametrize("mode", ["admins", "users"])
def test_a_message_mentions_as_many_as_its_button_says(mode):
    from telethon import types as tl

    from functions.broadcast import MENTION_ADMINS, MENTION_USERS, Broadcast
    from test_review_fixes import _Storage, _no_sleep, collect

    admin = tl.ChannelParticipantAdmin(user_id=0, promoted_by=0, date=None, admin_rights=tl.ChatAdminRights())
    members = [types.SimpleNamespace(id=i, participant=admin if i < 20 else None) for i in range(60)]
    mentioned = []

    class _Session:
        async def get_me(self):
            return types.SimpleNamespace(first_name="A")

        async def get_participants(self, peer, filter=None):
            return members[:20] if filter is not None else members

    fn = Broadcast(_Storage(), types.SimpleNamespace(delay=[0], messages_count=1, messages=["hi"]))
    fn.configure(choice=0, mention_all=True, mention_mode=mode,
                 content=types.SimpleNamespace(text="hi", entities=[], media=[], sent_media={}))
    fn.delay = _no_sleep

    async def send(session, peer, content, report, reply_to=None):
        mentioned.extend(e.user_id for e in content.entities)

    fn._send = send
    asyncio.run(fn.broadcast(_Session(), "chat", collect()[1]))
    assert len(mentioned) == (MENTION_ADMINS if mode == "admins" else MENTION_USERS)


def test_the_user_is_addressed_formally_everywhere():
    """Every text speaks to the user as «вы», never «ты»: no «ты/твой/тебя…», no «ты»-imperative."""
    import ast
    import glob
    import re

    informal = re.compile(
        r"\b(ты|тебе|тебя|тобой|твой|твоя|твоё|твое|твою|твои|твоих|твоим|твоей|твоего|твоему|"
        r"выбери|пришли|нажми|введи|отправь|укажи|запусти|добавь|открой|попробуй|подожди|напиши|дождись|"
        r"проверь|вставь|загрузи|перешли|скопируй|сохрани|удали|верни|продолжи|начни|включи|выключи|"
        r"отметь|задай|смени|измени|используй|убери|вступи|оставь|сделай|посмотри|перейди|переименуй|"
        r"собери|исправь|повтори|запроси|подтверди|отмени|привет)\b", re.IGNORECASE)
    found = []
    for path in sorted(glob.glob("bot/**/*.py", recursive=True) + glob.glob("functions/**/*.py", recursive=True)
                       + glob.glob("modules/**/*.py", recursive=True) + glob.glob("scraper/**/*.py", recursive=True)
                       + ["main.py"]):
        for node in ast.walk(ast.parse(open(path, encoding="utf-8").read())):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and informal.search(node.value):
                found.append(f"{path}:{node.lineno}: {informal.search(node.value).group()}")
    assert found == []
