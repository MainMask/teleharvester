"""The bot's side of the outreach analytics: the mailing's 🧪 step, 🧪 Гипотезы, ✍️ Ответить."""
import asyncio
import datetime
import json
from types import SimpleNamespace as ns

from bot.callbacks import ChoiceCB, HypothesisCB, ReplyCB
from bot.routers import hypotheses, mailing, replies
from bot.states import Hypotheses, ManualReply, PmMailing
from modules import outreach


class _State:
    def __init__(self, data=None):
        self.data, self.state = dict(data or {}), None

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kw):
        self.data.update(kw)

    async def set_state(self, state):
        self.state = state

    async def clear(self):
        self.data, self.state = {}, None


class _Msg:
    def __init__(self, text=""):
        self.text, self.answers, self.documents = text, [], []
        # as Telegram's: sent now, never edited (a confirmation's age: _common.confirm_expired)
        self.date, self.edit_date = datetime.datetime.now(datetime.timezone.utc), None

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs.get("reply_markup")))

    edit_text = reply = answer

    async def answer_document(self, document, **kwargs):
        self.documents.append(document)


class _Callback:
    def __init__(self):
        self.message, self.alerts = _Msg(), []

    async def answer(self, text=None, **kwargs):
        if text:
            self.alerts.append(text)


def _labels(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


# --- the mailing's 🧪 step ---

def test_the_step_offers_the_open_hypotheses_and_none(tmp_path):
    home = str(tmp_path)
    outreach.add_hypothesis(home, "старая")
    outreach.close_hypothesis(home, 1, "no")
    outreach.add_hypothesis(home, "открытая")
    callback, state = _Callback(), _State({"path": "x"})
    asyncio.run(mailing.mail_all(callback, state, ns(storage=ns(home=home))))
    (_, markup), = callback.message.answers
    assert _labels(markup) == ["#2 открытая", "Без гипотезы", "✖️ Отмена"]

    asyncio.run(mailing.mail_hypothesis(callback, ChoiceCB(scope="mail_hyp", value="2"), state))
    assert state.data["hypothesis"] == 2 and state.state == PmMailing.message


def test_a_hypotheses_file_broken_by_hand_leaves_the_mailing_under_none(tmp_path):
    """outreach.json edited by hand into bad JSON: the mailing goes on, under no hypothesis (the file never
    rewritten: its hypotheses would be lost), and 🧪 Гипотезы says what can't be read."""
    (tmp_path / outreach.STATE_NAME).write_text("{broken")
    callback, state = _Callback(), _State({"path": "x"})
    asyncio.run(mailing.mail_all(callback, state, ns(storage=ns(home=str(tmp_path)))))
    (text, markup), = callback.message.answers
    assert _labels(markup) == ["Без гипотезы", "✖️ Отмена"] and outreach.STATE_NAME in text

    screen = _Callback()
    asyncio.run(hypotheses.hypotheses_screen(screen.message, _State(), True, ns(storage=ns(home=str(tmp_path))),
                                             ns(project_id=3)))
    (text, markup), = screen.message.answers
    assert f"{outreach.STATE_NAME} не читается" in text and "⬅️ Назад" in _labels(markup)
    assert (tmp_path / outreach.STATE_NAME).read_text() == "{broken"


def test_a_typed_hypothesis_is_made_only_when_the_run_starts(tmp_path):
    home, state = str(tmp_path), _State({"path": mailing.project_base.WHOLE})
    asyncio.run(mailing.mail_new_hypothesis(_Msg("Вопрос в начале"), state))
    assert state.state == PmMailing.message and outreach.load(home)["hypotheses"] == []

    content = ns(text="Привет", media=[1, 2])
    run = mailing._start_run(ns(storage=ns(home=home)), state.data, content, 40)
    saved = outreach.load(home)
    assert saved["hypotheses"][0]["text"] == "Вопрос в начале"
    assert saved["runs"] == [{"id": run, "hypothesis": 1, "started": saved["runs"][0]["started"],
                              "segment": "📚 Вся база", "text": "Привет", "media": 2, "planned": 40}]


def test_a_hypothesis_deleted_after_it_was_picked_leaves_the_run_under_none(tmp_path):
    home = str(tmp_path)
    outreach.add_hypothesis(home, "удалят")
    outreach.delete_hypothesis(home, 1)
    mailing._start_run(ns(storage=ns(home=home)), {"hypothesis": 1, "recipients": ["@a"]}, ns(text="hi", media=[]), 1)
    run, = outreach.load(home)["runs"]
    assert run["hypothesis"] is None


def test_a_hypothesis_closed_after_it_was_picked_leaves_the_run_under_none(tmp_path):
    home = str(tmp_path)
    outreach.add_hypothesis(home, "закроют")
    outreach.close_hypothesis(home, 1, "no")
    mailing._start_run(ns(storage=ns(home=home)), {"hypothesis": 1, "recipients": ["@a"]}, ns(text="hi", media=[]), 1)
    run, = outreach.load(home)["runs"]
    assert run["hypothesis"] is None


def test_a_run_s_segment_names_its_list_or_file():
    assert mailing._segment({"recipients": ["@a", "@b"]}) == "список (2)"
    assert mailing._segment({"path": "assets/databases/G_participants.parquet"}) == "G_participants.parquet"


# --- 🧪 Гипотезы ---

def test_a_hypothesis_is_closed_with_its_verdict_and_insight(tmp_path):
    home = str(tmp_path)
    outreach.add_hypothesis(home, "Короткий оффер")
    pool, workspace = ns(storage=ns(home=home, usernames={})), ns(project_id=3)

    callback, state = _Callback(), _State()
    asyncio.run(hypotheses.close_ask(callback, HypothesisCB(action="close", id=1, project=3), state, pool, workspace))
    (_, markup), = callback.message.answers
    assert _labels(markup) == [*outreach.VERDICTS.values(), "✖️ Отмена"]

    asyncio.run(hypotheses.close_ask(callback, HypothesisCB(action="close", id=1, value="yes", project=3), state,
                                     pool, workspace))
    assert state.state == Hypotheses.insight
    assert _labels(callback.message.answers[-1][1]) == ["⏭ Без вывода", "✖️ Отмена"]  # ⏭, as every step's skip

    message = _Msg("работает, масштабируем")
    asyncio.run(hypotheses.close_with_insight(message, state, pool, workspace))
    closed, = outreach.load(home)["hypotheses"]
    assert (closed["verdict"], closed["insight"]) == ("yes", "работает, масштабируем")
    assert message.answers[0][0].startswith("🏁 Гипотеза #1 закрыта.") and state.state is None

    stale = _Callback()  # its 🏁 again, from an older screen
    asyncio.run(hypotheses.close_ask(stale, HypothesisCB(action="close", id=1, project=3), _State(), pool, workspace))
    assert stale.alerts == [hypotheses.CLOSED]


def test_the_screen_opens_each_hypothesis_s_card(tmp_path):
    home = str(tmp_path)
    for text in ("A", "B"):
        outreach.add_hypothesis(home, text)
    text, markup = hypotheses._view(home, 3)
    assert _labels(markup)[:3] == ["#2", "#1", "📄 Отчёт .md"]

    callback, workspace = _Callback(), ns(project_id=3)
    pool = ns(storage=ns(home=home))
    asyncio.run(hypotheses.card(callback, HypothesisCB(action="card", id=1, project=3), _State(), pool, workspace))
    (text, markup), = callback.message.answers
    assert "Гипотеза #1" in text and "отправок пока нет" in text
    # opened in the screen's place: ⬅️ Назад, as a menu opened in place goes back (a report's ⬅️ names the screen)
    assert _labels(markup) == ["🏁 Закрыть с итогом", "✏️ Изменить текст", "🗑 Удалить", "⬅️ Назад"]

    outreach.add_run(home, 1, "s", "t", 0, 1)  # a mailing under it: no 🗑
    callback = _Callback()
    asyncio.run(hypotheses.card(callback, HypothesisCB(action="card", id=1, project=3), _State(), pool, workspace))
    assert "🗑 Удалить" not in _labels(callback.message.answers[0][1])


def test_a_hypothesis_s_text_is_edited(tmp_path):
    home = str(tmp_path)
    outreach.add_hypothesis(home, "опечтка")
    pool, workspace, state = ns(storage=ns(home=home)), ns(project_id=3), _State()
    asyncio.run(hypotheses.edit_ask(_Callback(), HypothesisCB(action="edit", id=1, project=3), state, pool))
    assert state.state == Hypotheses.text
    message = _Msg("без опечатки")
    asyncio.run(hypotheses.edit_apply(message, state, pool, workspace))
    assert outreach.find_hypothesis(home, 1)["text"] == "без опечатки" and state.state is None
    assert message.answers[0][0].startswith("✅ Текст гипотезы изменён.")


def test_a_hypothesis_is_deleted_once_asked_unless_a_mailing_ran_under_it(tmp_path):
    home = str(tmp_path)
    outreach.add_hypothesis(home, "лишняя")
    outreach.add_hypothesis(home, "с рассылкой")
    outreach.add_run(home, 2, "s", "t", 0, 1)
    pool, workspace = ns(storage=ns(home=home)), ns(project_id=3)

    asked = _Callback()
    asyncio.run(hypotheses.delete(asked, HypothesisCB(action="delete", id=1, project=3), pool, workspace))
    # on one row, as every confirmation
    assert [[b.text for b in row] for row in asked.message.answers[0][1].inline_keyboard] == \
        [["🗑 Да, удалить", "✖️ Отмена"]]
    assert outreach.find_hypothesis(home, 1) is not None

    late = _Callback()  # its «Да» an hour later: nothing deleted
    late.message.date -= datetime.timedelta(hours=1)
    asyncio.run(hypotheses.delete(late, HypothesisCB(action="delete", id=1, value="yes", project=3), pool, workspace))
    assert late.alerts == ["Подтверждение устарело — откройте гипотезу заново."]
    assert outreach.find_hypothesis(home, 1) is not None

    done = _Callback()
    asyncio.run(hypotheses.delete(done, HypothesisCB(action="delete", id=1, value="yes", project=3), pool, workspace))
    assert outreach.find_hypothesis(home, 1) is None and done.message.answers[0][0].startswith("🗑 Гипотеза #1 удалена.")

    refused = _Callback()
    asyncio.run(hypotheses.delete(refused, HypothesisCB(action="delete", id=2, value="yes", project=3), pool,
                                  workspace))
    assert refused.alerts == [hypotheses.HAS_RUNS] and outreach.find_hypothesis(home, 2) is not None


def test_the_report_is_a_md_document(tmp_path):
    home = str(tmp_path)
    outreach.add_hypothesis(home, "Короткий оффер")
    callback = _Callback()
    asyncio.run(hypotheses.report(callback, ns(storage=ns(home=home, usernames={})), ns(project_id=3)))
    document, = callback.message.documents
    assert document.filename.endswith(".md") and "Короткий оффер" in document.data.decode()


def test_the_report_counts_who_was_mailed_before_the_first_run(tmp_path):
    (tmp_path / "pm_mailing.json").write_text(
        '{"1": {"count": 1, "last_date": "2020-01-01 10:00:00"}, "2": {"gone": "2020-01-01 10:00:00"}}')
    assert hypotheses._earlier(str(tmp_path)) == 1
    outreach.add_run(str(tmp_path), None, "s", "t", 0, 1)
    assert hypotheses._earlier(str(tmp_path)) == 1  # the run started after it


def test_a_report_on_a_ledger_broken_by_hand_says_so(tmp_path):
    (tmp_path / "pm_mailing.json").write_text("{broken")
    callback = _Callback()
    asyncio.run(hypotheses.report(callback, ns(storage=ns(home=str(tmp_path), usernames={})), ns(project_id=3)))
    assert not callback.message.documents and "pm_mailing.json" in callback.message.answers[0][0]


# --- ✍️ Ответить ---

class _Client:
    def __init__(self, path, cached=False, fail=None):
        self.path, self.cached, self.fail, self.sent = path, cached, fail, []

    async def get_input_entity(self, user):
        if not self.cached:
            raise ValueError("Could not find the input entity")
        return ns(id=user)

    async def iter_dialogs(self, limit):
        for user_id in (4, 5):
            yield ns(entity=ns(id=user_id))

    async def send_message(self, entity, text, parse_mode="md"):
        if self.fail:
            raise self.fail
        self.sent.append((entity.id, text, parse_mode))


def _reply_pool(client, scraped=False, home=None):
    return ns(workers=[client], scraped=lambda path: scraped,
              storage=ns(directory="/p/sessions", usernames={"/p/sessions/w1.jsession": "worker1"},
                         get_session_path=lambda c: c.path, home=home))


def _no_borrow(monkeypatch):
    async def borrow(pool, client, path, work, timeout):
        return await work()
    monkeypatch.setattr(replies, "borrow", borrow)


def test_an_answer_goes_from_the_worker_the_person_replied_to(monkeypatch, tmp_path):
    _no_borrow(monkeypatch)
    client = _Client("/p/sessions/w1.jsession")
    pool, callback, state = _reply_pool(client, home=str(tmp_path)), _Callback(), _State()
    asyncio.run(replies.reply_start(callback, ReplyCB(project=1, worker="w1.jsession", user=5), state, pool))
    assert state.state == ManualReply.text and "@worker1" in callback.message.answers[0][0]

    message = _Msg("Добрый день! Расскажу подробнее")
    asyncio.run(replies.reply_send(message, state, pool, ns(project_id=1)))
    assert client.sent == [(5, "Добрый день! Расскажу подробнее", None)]  # found among its chats, sent as typed
    (text, markup), = message.answers
    assert text == "✅ Отправлено от @worker1." and state.state is None
    assert _labels(markup) == ["⬅️ 📬 Автоответ"]  # the flow's last word: never a dead end
    logged, = [json.loads(line) for line in (tmp_path / outreach.DIALOGS_NAME).read_text().splitlines()]
    assert (logged["worker"], logged["user"], logged["dir"], logged["auto"]) == ("w1.jsession", 5, "out", False)


def test_a_worker_gone_scraping_or_refused_is_said(monkeypatch):
    _no_borrow(monkeypatch)
    gone = _Callback()
    asyncio.run(replies.reply_start(gone, ReplyCB(project=1, worker="other.jsession", user=5), _State(),
                                    _reply_pool(_Client("/p/sessions/w1.jsession"))))
    assert gone.alerts == [replies.WORKER_GONE]

    data = {"reply_worker": "w1.jsession", "reply_user": 5}
    client = _Client("/p/sessions/w1.jsession", cached=True)
    state, message = _State(data), _Msg("ответ")
    state.state = ManualReply.text
    asyncio.run(replies.reply_send(message, state, _reply_pool(client, scraped=True), ns(project_id=1)))
    assert client.sent == [] and "занят скрапом" in message.answers[0][0] and state.state == ManualReply.text

    client.fail = ValueError("USER_IS_BLOCKED")
    asyncio.run(replies.reply_send(message, state, _reply_pool(client), ns(project_id=1)))
    assert "Не отправлено от @worker1" in message.answers[1][0] and state.state == ManualReply.text

    # a job's end released the worker mid-send: Telethon cancels its pending request — said, the step stays
    client.fail = asyncio.CancelledError()
    asyncio.run(replies.reply_send(message, state, _reply_pool(client), ns(project_id=1)))
    assert "соединение прервано" in message.answers[2][0] and state.state == ManualReply.text

    message = _Msg("ответ")  # the worker moved out since ✍️: said, with ⬅️
    asyncio.run(replies.reply_send(message, _State({"reply_worker": "other.jsession", "reply_user": 5}),
                                   _reply_pool(client), ns(project_id=1)))
    assert message.answers == [(replies.WORKER_GONE, message.answers[0][1])]
    assert _labels(message.answers[0][1]) == ["⬅️ 📬 Автоответ"]


def test_a_mailing_that_does_not_start_leaves_no_run_nor_new_hypothesis(monkeypatch, tmp_path):
    class _Manager:  # no workers: the job never runs
        active, project_id = False, 1

        async def run(self, *args, **kwargs):
            return False

    async def content(message, album):
        return ns(text="hi", media=[], cleanup=lambda: None)

    instance = ns(load_stats=lambda: None, drop_gone=lambda rows: rows)
    monkeypatch.setattr(mailing, "resolve", lambda functions, key: (instance, ns(risk="risky")))
    monkeypatch.setattr(mailing, "build_content", content)
    state = _State({"recipients": ["@a"], "skip": False, "limit": None, "hypothesis_text": "новая"})
    asyncio.run(mailing.mail_run(ns(answer=_Msg().answer, chat=ns(id=1), bot=None), state, None,
                                 ns(storage=ns(home=str(tmp_path))), {}, _Manager(), None))
    assert outreach.load(str(tmp_path)) == {"hypotheses": [], "runs": []}


def test_a_hypothesis_deleted_while_its_insight_was_typed_is_said_gone_not_closed(tmp_path):
    home = str(tmp_path)
    pool, workspace = ns(storage=ns(home=home)), ns(project_id=3)
    state = _State({"hypothesis": 1, "verdict": "yes"})
    message = _Msg("вывод")
    asyncio.run(hypotheses.close_with_insight(message, state, pool, workspace))  # no #1 by now
    assert message.answers[0][0].startswith(hypotheses.GONE)


def test_a_card_s_button_on_a_hypotheses_file_broken_by_hand_says_so(tmp_path, monkeypatch):
    """Through the real dispatcher: 🧪's error handler, not a tap left unanswered; the file never rewritten."""
    from aiogram import Bot, Dispatcher
    from aiogram.methods import AnswerCallbackQuery
    from aiogram.types import CallbackQuery, Chat, Message, Update, User

    (tmp_path / outreach.STATE_NAME).write_text("{broken")
    calls = []

    class Offline(Bot):
        async def __call__(self, method, request_timeout=None):
            calls.append(method)

    monkeypatch.setattr(hypotheses.router, "_parent_router", None)  # included again, in this dispatcher
    dp = Dispatcher(pool=ns(storage=ns(home=str(tmp_path))), workspace=ns(project_id=3))
    dp.include_router(hypotheses.router)
    user = User(id=1, is_bot=False, first_name="x")
    message = Message(message_id=1, date=datetime.datetime.now(), chat=Chat(id=1, type="private"), from_user=user,
                      text="…")
    try:
        for action in ("card", "close", "edit", "delete"):
            calls.clear()
            callback = CallbackQuery(id="1", from_user=user, chat_instance="1", message=message,
                                     data=HypothesisCB(action=action, id=1, project=0).pack())
            asyncio.run(dp.feed_update(Offline("42:TEST"), Update(update_id=1, callback_query=callback)))
            (answer,) = calls
            assert isinstance(answer, AnswerCallbackQuery) and answer.show_alert, action
            assert f"{outreach.STATE_NAME} не читается" in answer.text, action
    finally:
        hypotheses.router._parent_router = None
    assert (tmp_path / outreach.STATE_NAME).read_text() == "{broken"
