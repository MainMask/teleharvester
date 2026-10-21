"""Each project's own workers and job slots in the bot (bot/services/workspaces.py): side by side, the chat's
current project's given to its handlers, an account moved between them, a free one taken in."""

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace as ns

from bot.callbacks import ChoiceCB, FunctionCB, MenuAction, MenuCB, NavCB
from bot.routers import accounts, menu, stale
from bot.services.workspaces import NO_PROJECT, PROJECT_GONE, ProjectMiddleware, Workspaces
from modules import projects, restricted_workers
from test_sync import _Callback, _Msg, _SCallback, _State
from test_worker_order import _write_jsession

SETTINGS = ns(api_id=1, api_hash="h")


def _project(name, phones=()):
    project = projects.add(name, [])
    os.makedirs(projects.sessions_dir(project))
    for phone in phones:
        _write_jsession(Path(projects.sessions_dir(project)), phone, f"w{phone}")
    return project


def _workspaces(*projects_):
    workspaces = Workspaces(SETTINGS, {})
    for project in projects_:
        workspaces.add(project)
    return workspaces


def test_each_project_has_its_own_workers_and_slots():
    green, blue = _project("Green", ["111", "112"]), _project("Blue", ["221"])
    workspaces = _workspaces(green, blue)
    a, b = workspaces.get(green.id), workspaces.get(blue.id)
    assert (a.pool.count(), b.pool.count(), workspaces.workers()) == (2, 1, 3)
    assert a.pool.storage.home == projects.home(green)  # its stats, ledger and proxies

    # a job in one project holds its slot only: the other's starts
    assert a.manager.acquire("Рассылка", timeout=0) and not a.manager.acquire("Ещё", timeout=0)
    assert b.manager.acquire("Рассылка", timeout=0)
    assert workspaces.slot(b.manager.key) is b.manager and workspaces.slot(a.scrapes.key) is a.scrapes
    assert workspaces.username(os.path.join(projects.sessions_dir(blue), "221.jsession")) == "w221"


def test_a_personal_account_scraped_in_one_project_is_busy_in_every_one():
    green, blue = _project("Green"), _project("Blue")
    workspaces = _workspaces(green, blue)
    a, b = workspaces.get(green.id).pool, workspaces.get(blue.id).pool
    a.scraping = ns(path="personal_sessions/me.jsession")
    a.borrowed["personal_sessions/other.jsession"] += 1
    assert b.scraped("personal_sessions/me.jsession") and b.busy("personal_sessions/other.jsession")
    workspaces.remove(green.id)
    assert not b.scraped("personal_sessions/me.jsession")  # a deleted project's pool is gone


def test_the_chat_s_current_project_is_given_to_its_handlers():
    green = _project("Green", ["111"])
    workspaces = _workspaces()
    middleware = ProjectMiddleware(workspaces)
    seen, alerts = [], []

    async def handler(event, data):
        seen.append(data)

    async def answer(text, **kwargs):
        alerts.append((text, kwargs.get("show_alert")))

    message = ns(chat=ns(id=5), answer=answer)
    asyncio.run(middleware(handler, message, {}))  # no project opened yet: the handler doesn't run
    assert seen == [] and alerts == [(NO_PROJECT, None)]

    workspace = workspaces.open(5, green)  # its card opened in this chat
    asyncio.run(middleware(handler, message, {}))
    assert seen[0]["pool"] is workspace.pool and seen[0]["manager"] is workspace.manager
    assert workspace.pool.count() == 1  # added when first opened: read from its folder

    # one that finds its project by itself runs in no project too (the 24 h ⛔ release button)
    asyncio.run(middleware(handler, ns(chat=ns(id=6), answer=answer), {"handler": ns(flags={"any_project": True})}))
    assert len(seen) == 2


def _through(middleware, data, state, chat=5):
    """A button's tap through the middleware: (the workspace its handler got, or None if it didn't run; the
    pop-ups)."""
    seen, alerts = [], []

    async def handler(event, data):
        seen.append(data["workspace"])

    async def answer(text, **kwargs):
        alerts.append(text)

    asyncio.run(middleware(handler, ns(chat=ns(id=chat), answer=answer), {"callback_data": data, "state": state}))
    return (seen or [None])[0], alerts


def test_a_project_s_button_acts_on_its_own_project_whichever_the_chat_is_in(monkeypatch):
    """An old screen's button of Green, tapped while the chat is in Blue: Green's workers, not Blue's."""
    from bot.services import login as logins

    closed = []

    async def close(chat_id):
        closed.append(chat_id)

    monkeypatch.setattr(logins, "close", close)
    green, blue = _project("Green", ["111"]), _project("Blue", ["221"])
    workspaces = _workspaces(green, blue)
    middleware = ProjectMiddleware(workspaces)
    workspaces.open(5, blue)  # Blue's card opened since
    for data in (FunctionCB(key="reactions", project=green.id), MenuCB(action=MenuAction.LIST, project=green.id),
                 NavCB(screen="💬 Активность", project=green.id)):
        workspaces.open(5, blue)
        state = _State({"link": "t.me/blue/1"})  # a form under way in Blue
        state.state = "Reactions:link"
        workspace, alerts = _through(middleware, data, state)
        assert workspace is workspaces.get(green.id) and workspaces.current[5] == green.id and not alerts
        assert state.state is None and state.data == {}  # Blue's form left: its steps would act on Green now
    assert closed == [5, 5, 5]  # a sign-in by phone waiting for its code: Blue's, closed

    # one of the current project: nothing left
    state = _State({"link": "t.me/green/1"})
    state.state = "Reactions:link"
    assert _through(middleware, MenuCB(action=MenuAction.LIST, project=green.id), state)[0] is workspaces.get(green.id)
    assert state.state == "Reactions:link"


def test_a_button_of_a_project_deleted_since_says_so():
    green = _project("Green")
    workspaces = _workspaces(green)
    workspaces.open(5, green)
    workspace, alerts = _through(ProjectMiddleware(workspaces), MenuCB(action=MenuAction.LIST, project=99), _State())
    assert workspace is None and alerts == [PROJECT_GONE] and workspaces.current[5] == green.id


def test_status_shows_every_project_s_jobs_by_name_and_stops_each_by_its_button():
    green, blue = _project("Green"), _project("Blue")
    workspaces = _workspaces(green, blue)
    workspaces.get(green.id).manager.acquire("Рассылка", timeout=0)
    workspaces.get(blue.id).analyses.acquire("Анализ", timeout=0)
    msg = _Msg("/status")
    asyncio.run(menu.status(msg, workspaces))
    assert [text for text, _ in msg.answers] == ["📊 Задачи — выполняется: 2\n\n▶️ 📁 Green · Рассылка",
                                                 "▶️ 📁 Blue · Анализ"]
    # a dialog that can be left (as the report's choice: both acquired so here) has its ⏹ too
    assert [markup is not None for _, markup in msg.answers] == [True, True]

    answers = []
    callback = ns(answer=lambda text, **k: answers.append(text) or asyncio.sleep(0))
    key = workspaces.get(green.id).manager.key
    asyncio.run(menu.stop_job(callback, ChoiceCB(scope="job_stop", value=str(key)), workspaces))
    assert answers == ["Останавливаю…"] and not workspaces.get(green.id).manager.active
    assert workspaces.get(blue.id).analyses.active  # the other project's job runs on


def _tap(handler, scope, value, state, *deps):
    """A button of a ticks list (its scope, value); the callback, with what it answered and edited."""
    callback, answers = _SCallback(), []

    async def answer(text=None, **kwargs):
        answers.append(text)

    callback.answer = answer
    asyncio.run(handler(callback, ChoiceCB(scope=scope, value=value), state, *deps))
    return callback, answers


def _ticks(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def test_workers_are_ticked_and_moved_to_another_project_with_their_restrictions():
    green, blue = _project("Green", ["111", "112", "113"]), _project("Blue")
    workspaces = _workspaces(green, blue)
    source, target = workspaces.get(green.id), workspaces.get(blue.id)
    old = os.path.join(projects.sessions_dir(green), "111.jsession")
    restricted_workers.save([old])
    source.pool.polling = os.path.join(projects.sessions_dir(green), "113.jsession")  # the auto-reply polls it now
    state, callback = _State(), _SCallback()
    asyncio.run(accounts.move.move_start(callback, state, source.pool))
    text, markup = callback.message.edits[0]  # in 🤖 Аккаунты' place
    assert text.endswith("Отмечено: 0 из 3")
    assert _ticks(markup) == ["⬜ W (@w111)", "⬜ W (@w112)", "⬜ W (@w113) ⏳ занят", "☑️ Все", "⬜ Никого", "▶️ Далее",
                              "✖️ Отмена"]

    _, answers = _tap(accounts.move.move_pick, "mv_out", "go", state, source)
    assert answers == ["Отметьте хотя бы один аккаунт"]  # nothing ticked yet
    callback, _ = _tap(accounts.move.move_pick, "mv_out", "all", state, source)
    text, markup = callback.message.edits[0]
    assert text.endswith("Отмечено: 3 из 3") and _ticks(markup)[0] == "✅ W (@w111)"
    callback, _ = _tap(accounts.move.move_pick, "mv_out", "go", state, source)
    text, markup = callback.message.edits[0]
    assert text == "Куда перенести отмеченных — 3?" and _ticks(markup) == ["📁 Blue", "📦 В свободные", "✖️ Отмена"]

    callback, _ = _tap(accounts.move.move_to, "mv_to", str(blue.id), state, source.pool, source, workspaces)
    assert callback.message.edits[0][0] == ("✅ Перенесено в проект «Blue»: 2 из 3.\n\nНе перенесены:\n"
                                            "• W (@w113) — занят задачей — перенесите после её окончания")
    new = os.path.join(projects.sessions_dir(blue), "111.jsession")
    assert (source.pool.count(), target.pool.count()) == (1, 2) and restricted_workers.load() == [new]
    assert state.state is None  # the flow is over: its buttons are stale now
    answers = []  # the last router's (stale.py)
    asyncio.run(stale.stale_button(ns(answer=lambda text: answers.append(text) or asyncio.sleep(0))))
    assert answers == ["Кнопка устарела — откройте раздел заново."]


def test_free_accounts_are_ticked_into_the_project():
    green = _project("Green")
    os.makedirs(projects.FREE_DIR)
    for phone in ("331", "332", "333"):
        _write_jsession(Path(projects.FREE_DIR), phone, f"free{phone}")
    workspace = _workspaces(green).get(green.id)
    state, callback = _State(), _SCallback()
    asyncio.run(accounts.move.free_start(callback, state))
    text, markup = callback.message.edits[0]  # in ➕ Добавить's place
    assert "Свободные аккаунты</b>" in text and text.endswith("Отмечено: 0 из 3")
    assert _ticks(markup)[:3] == ["⬜ @free331 · +331", "⬜ @free332 · +332", "⬜ @free333 · +333"]

    for index in ("0", "2"):
        _tap(accounts.move.free_pick, "mv_free", index, state, workspace.pool, workspace)
    callback, _ = _tap(accounts.move.free_pick, "mv_free", "go", state, workspace.pool, workspace)
    assert callback.message.edits[0][0] == "✅ Добавлено в проект: 2 из 2.\n🤖 Воркеров в проекте: 2"
    assert projects.account_files(projects.FREE_DIR) == [os.path.join(projects.FREE_DIR, "332.jsession")]


def test_a_project_with_a_job_or_with_workers_is_not_deleted():
    from bot.routers.projects import manage as router

    green = _project("Green", ["111"])
    workspaces = _workspaces(green)
    state = _State({"delete_name": "Green", "delete_token": "t"})
    workspaces.get(green.id).scrapes.acquire("Скрап", timeout=0)
    callback = _Callback()
    asyncio.run(router.project_delete(callback, ChoiceCB(scope="prj_del", value="yt"), state, workspaces))
    assert callback.alerts == ["⛔ Идёт «Скрап» — удалите проект после окончания."] and not callback.message.answers

    workspaces.get(green.id).scrapes.release()
    callback = _SCallback()
    asyncio.run(router.project_delete(callback, ChoiceCB(scope="prj_del", value="yt"), state, workspaces))
    assert "аккаунтов: 1" in callback.message.answers[0][0] and workspaces.get(green.id) is not None
    assert [p.name for p in projects.load()] == ["Green"]


def test_a_personal_account_another_project_scrapes_on_is_refused():
    from bot.routers.scraping import common
    from bot.services.jobs import JobManager

    green, blue = _project("Green"), _project("Blue")
    workspaces = _workspaces(green, blue)
    workspaces.get(green.id).pool.scraping = ns(path="personal_sessions/me.jsession")
    said = []

    async def say(text):
        said.append(text)

    account = ns(path="personal_sessions/me.jsession", personal=True)
    pool = workspaces.get(blue.id).pool
    assert asyncio.run(common.take_slot(JobManager(), pool, account, "Скрап", say)) is None
    assert said == [common.PERSONAL_SCRAPING] and pool.scraping is None
