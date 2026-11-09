"""The menu's flows, alike everywhere: a prompt never offers a button it has not, a button never acts on
another project, a busy job slot keeps the step, a stale button is said in a pop-up."""

import asyncio
import time

import conftest
from types import SimpleNamespace as ns

from bot.callbacks import ChoiceCB, NavCB
from bot.routers._common import STALE_BUTTON
from bot.states import AddByPhone, Invite, PollVote, ProfileText, Reactions
from modules.registry import ACTIVITY, COLLECT
from test_sync import _buttons, _Callback, _launches, _Msg, _State

BUSY = ns(active=True, label="Рассылка", project_id=7)


def test_the_combine_tool_s_hint_reaches_telegram_as_valid_html(monkeypatch):
    from bot.routers.scraping import analysis
    from modules import scraped_files

    monkeypatch.setattr(scraped_files, "scrape_posts", lambda project: [])
    state = _State({"only_project": "P", "analysis_project": 7, "analysis_token": "t"})
    callback = _Callback()
    asyncio.run(analysis.analysis_tool(callback, ChoiceCB(scope="an_tool", value="combine.t"), state))
    text, markup = callback.message.answers[0]
    # «<имя>» unescaped is an unknown tag to Telegram: the message would never be sent
    assert "&lt;имя&gt;_posts_combined" in text and "<имя>" not in text
    assert "Нажмите кнопку" not in text and _buttons(markup) == ["✖️ Отмена"]  # no button to press


def test_the_analysis_menu_leads_back_to_its_own_project():
    from bot.routers.scraping import analysis

    msg = _Msg()
    asyncio.run(analysis.analysis_project(msg, _State(), ns(name="P", id=7)))
    text, markup = msg.answers[0]
    assert text.startswith(f"📁 <b>P</b> › <b>{COLLECT}</b> › <b>📈 Анализ</b>")
    back = NavCB.unpack(markup.inline_keyboard[-1][0].callback_data)
    assert (back.screen, back.project) == (COLLECT, 7)  # never whichever project is current later


def test_an_old_resend_button_at_the_password_step_is_stale():
    from bot.routers import login

    state = _State({"role": "worker"})
    state.state = AddByPhone.password
    callback = _Callback()
    asyncio.run(login.phone_resend(callback, state))
    assert callback.alerts == [STALE_BUTTON] and callback.message.answers == []
    assert state.state == AddByPhone.password  # the sign-in stays at its password, no new code asked


def test_a_poll_option_beyond_the_poll_s_is_asked_again():
    from bot.routers import activity

    state = _State({"link": "https://t.me/c/1/2", "options": 3})
    state.state = PollVote.option
    msg = _Msg(text="7")
    asyncio.run(activity.poll_run(msg, state, None, {}, ns(active=False)))
    assert msg.answers == [("Такого варианта нет: в опросе их 3. Пришлите ещё раз:", None)]
    assert state.state == PollVote.option


def test_the_messages_count_prompt_offers_to_keep_only_a_count_there_is():
    from bot.routers import chat_broadcasts

    for count, keep in ((0, False), (5, True)):
        said = []
        settings = ns(messages_count=count)
        asyncio.run(chat_broadcasts._ask_count(lambda text, **kw: said.append((text, kw["reply_markup"]))
                                               or asyncio.sleep(0), _State(), settings))
        (text, markup), = said
        assert ("оставьте текущее" in text) == keep
        assert any(label.startswith("✅ Оставить") for label in _buttons(markup)) == keep


def test_a_list_button_of_an_emptied_file_says_so(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from bot.routers import profile

    state = _State({"pick_key": "name"})  # the region's names.txt: none (conftest's empty content folder)
    state.state = ProfileText.text
    manager, runs = _launches()
    callback = _Callback()
    pool = SimpleNamespace(storage=SimpleNamespace(home=conftest.home()))
    asyncio.run(profile.text_from_file(callback, state, pool, {}, manager))
    assert runs == [] and "пуст или не найден" in callback.alerts[0]
    assert state.state == ProfileText.text  # a text or a .txt can still be sent


def test_a_busy_slot_keeps_a_typed_last_step():
    from bot.routers import invite

    state = _State({"source": "@from"})
    state.state = Invite.destination
    msg = _Msg(text="@to")
    asyncio.run(invite.got_destination(msg, state, None, {}, BUSY))
    (text, markup), = msg.answers
    assert text.startswith("⛔ Занято: идёт «Рассылка»") and text.endswith("пришлите ссылку ещё раз.")
    assert _buttons(markup) == [f"⬅️ {ACTIVITY}"]
    assert state.state == Invite.destination and state.data == {"source": "@from"}  # nothing typed is lost


def test_a_busy_slot_keeps_a_last_button_s_step():
    from bot.routers import activity

    state = _State({"link": "https://t.me/c/1/2"})
    state.state = Reactions.link
    callback = _Callback()
    asyncio.run(activity.reactions_run(callback, ChoiceCB(scope="reaction", value="🔥"), state, None, {}, BUSY))
    assert callback.alerts[0].startswith("⛔ Занято: идёт «Рассылка»") and callback.message.answers == []
    assert state.state == Reactions.link  # its buttons can be tapped again once that job is over


def test_a_job_s_time_is_shown_past_an_hour():
    from bot.services.runner import TelegramReporter

    reporter = TelegramReporter(None, 1, job_label="x")
    reporter._started = time.monotonic() - 3 * 3600
    assert "⏱ Время: 00:03:00:00" in reporter._build_report("✅ Готово")


def test_the_last_router_says_a_stale_button_in_a_pop_up():
    from bot.routers import stale

    said = []
    asyncio.run(stale.stale_button(ns(answer=lambda text, **kw: said.append((text, kw)) or asyncio.sleep(0))))
    assert said == [(STALE_BUTTON, {"show_alert": True})]  # as every other stale tap (stale_tap)


def test_the_start_notice_brings_the_main_menu_back(tmp_path, monkeypatch):
    from bot import app
    from bot.keyboards.menu import main_menu

    monkeypatch.chdir(tmp_path)
    sent = []

    async def send_message(chat_id, text, **kwargs):
        sent.append((text, kwargs))

    asyncio.run(app.announce_start(ns(send_message=send_message), {1}, ns(all=lambda: [], workers=lambda: 0)))
    (text, kwargs), = sent
    assert kwargs["reply_markup"] == main_menu() and kwargs["parse_mode"] == "HTML"  # hidden while it was down


def test_an_earlier_report_s_options_never_touch_the_running_one():
    from bot.routers import moderation

    stopped = []
    manager = ns(stop=lambda: stopped.append(True) or asyncio.sleep(0), project_id=7, key=0)
    moderation._FLOWS[(1, 0)] = (ns(step=None), {"busy": False, "selections": [], "menu": 20}, [ns(option=b"a")])
    try:
        for value in (moderation.STOP, "0"):  # ✖️ and an option of the report before
            callback = _Callback()
            callback.message.message_id = 10
            asyncio.run(moderation.rm_choose(callback, ChoiceCB(scope="report_opt", value=value), manager, None))
            assert callback.alerts == [STALE_BUTTON] and callback.message.answers == []
        assert stopped == [] and moderation._FLOWS[(1, 0)][1]["selections"] == []
    finally:
        moderation._FLOWS.clear()


def test_the_scrape_s_run_button_is_pressed_once():
    from bot.routers.scraping import scrape

    callback = _Callback()
    removed = []

    async def edit_reply_markup(reply_markup=None):
        removed.append(reply_markup)

    callback.message.edit_reply_markup = edit_reply_markup
    callback.bot = None
    # its data gone (a bad value): the run is refused, the ▶️ is gone all the same
    asyncio.run(scrape._start_scrape(callback, _State(), None, None, ns(project_id=7)))
    assert removed == [None]


def test_a_long_list_keeps_whole_lines_only_so_an_escaped_name_is_never_cut():
    from bot.routers._common import MAX_MESSAGE, fit_lines

    lines = [f"• @worker{i} — {'&quot;ошибка&quot; ' * 20}" for i in range(100)]  # each ~330 characters
    text = fit_lines(lines, more=5)
    shown = text.split("\n")
    assert len(text) <= MAX_MESSAGE and all(line in lines for line in shown[:-1])  # none cut midway
    assert shown[-1] == f"…и ещё {100 - (len(shown) - 1) + 5}"  # the ones left out, those before too
    assert fit_lines(["a", "b"]) == "a\nb" and fit_lines(["a"], more=3) == "a\n…и ещё 3"
