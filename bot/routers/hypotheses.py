"""🧪 Гипотезы (🎯 База и рассылка → 🧪 Гипотезы, its view): the project's mailing hypotheses (modules/outreach.py) with
their numbers, each closed with its verdict and insight; the whole as a .md report, for a person or an AI to read.

A hypothesis is made where it is acted on: the 📨 Рассылка's step (bot/routers/mailing.py)."""
import asyncio
import html
import os
from datetime import datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message

from bot.callbacks import ChoiceCB, HypothesisCB
from bot.keyboards.common import button, cancel_kb, choice_kb
from bot.keyboards.menu import nav_button, screen_kb
from bot.routers._common import fit_lines, require_text
from bot.services import screens
from bot.services.delegation import WorkerPool
from bot.services.workspaces import Workspace
from bot.states import Hypotheses
from modules import outreach, projects
from modules.registry import HYPOTHESES

router = Router()

SHOWN_TEXT = 120  # of a hypothesis's text, on the screen
CARD_BUTTONS = 20  # the newest hypotheses get a button to their card on the screen
GONE = "Такой гипотезы больше нет."
CLOSED = "Гипотеза уже закрыта."
HAS_RUNS = "Под этой гипотезой уже шла рассылка — её цифры нужны в отчёте. Закройте её вместо удаления."


def _numbers_line(numbers: dict | None) -> str:
    if not numbers:
        return "отправок пока нет"
    return outreach.with_flags(outreach.summary(numbers), numbers)


def _line(hypothesis: dict | None, numbers: dict | None) -> str:
    """A hypothesis on the screen (None: the runs under none): its text, numbers, status."""
    if hypothesis is None:
        return f"<b>{outreach.NO_HYPOTHESIS}</b>\n    {_numbers_line(numbers)}"
    text = hypothesis["text"]
    text = text[:SHOWN_TEXT] + ("…" if len(text) > SHOWN_TEXT else "")
    numbers_line = f"{_numbers_line(numbers)} · {outreach.status(hypothesis)}"
    return f"<b>#{hypothesis['id']}</b> {html.escape(text)}\n    {numbers_line}"


def _view(home: str, project: int):
    """The screen's text and buttons; reads the logs, so off the event loop."""
    state, numbers = outreach.load(home), outreach.stats(home)
    hypotheses = state["hypotheses"][::-1]  # the newest first
    lines = [_line(h, numbers.get(h["id"])) for h in hypotheses]
    if None in numbers:
        lines.append(_line(None, numbers[None]))
    intro = ("Гипотеза задаётся на шаге «🧪» при запуске 📨 Рассылки. "
             f"Метрика — доля ответивших в течение {outreach.REPLY_HOURS} ч. "
             "Откройте гипотезу кнопкой с её номером: закрыть с итогом и выводом, исправить, удалить. "
             "📄 Отчёт .md — все гипотезы с диалогами, для разбора, в том числе с AI.")
    body = fit_lines(lines, limit=3000) if lines else "Гипотез пока нет."
    text = f"{screens.title_line(HYPOTHESES, screens.project_name(project))}\n\n{intro}\n\n{body}"

    shown = hypotheses[:CARD_BUTTONS]
    own = [[button(f"#{h['id']}", HypothesisCB(action="card", id=h["id"], project=project)) for h in shown[i:i + 4]]
           for i in range(0, len(shown), 4)]
    own.append([button("📄 Отчёт .md", HypothesisCB(action="report", project=project))])
    return text, screen_kb(HYPOTHESES, project, own=own)


def _noted(note: str, text: str) -> str:
    """The screen's text under a line on what was just done, if any."""
    return f"{html.escape(note)}\n\n{text}" if note else text


async def _show(message: Message, edit: bool, pool: WorkerPool, project: int, note: str = ""):
    text, markup = await asyncio.to_thread(_view, pool.storage.home, project)
    await screens.send(message, edit)(_noted(note, text), parse_mode="HTML", reply_markup=markup)


@screens.view(HYPOTHESES)
async def hypotheses_screen(message: Message, state: FSMContext, edit: bool, pool: WorkerPool, workspace: Workspace,
                            **_):
    await _show(message, edit, pool, workspace.project_id)


def _card_view(home: str, number: int, project: int):
    """A hypothesis's card: its text, numbers, verdict and insight; 🏁 while open, ✏️, 🗑 while no mailing ran under
    it, ⬅️ to the screen. None if it is gone. Reads the logs, so off the event loop."""
    hypothesis = outreach.find_hypothesis(home, number)
    if hypothesis is None:
        return None
    lines = [f"🧪 <b>Гипотеза #{number}</b> — {outreach.status(hypothesis)}",
             f"<blockquote>{html.escape(hypothesis['text'])}</blockquote>",
             _numbers_line(outreach.stats(home).get(number))]
    if hypothesis.get("insight"):
        lines.append(f"<b>Вывод:</b> {html.escape(hypothesis['insight'])}")

    def act(label: str, action: str):
        return [button(label, HypothesisCB(action=action, id=number, project=project))]

    rows = [*([act("🏁 Закрыть с итогом", "close")] if not hypothesis.get("closed") else []),
            act("✏️ Изменить текст", "edit"),
            *([act("🗑 Удалить", "delete")] if not outreach.has_runs(home, number) else []),
            [nav_button(f"⬅️ {HYPOTHESES}", HYPOTHESES, project)]]
    return "\n\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_card(message: Message, edit: bool, pool: WorkerPool, number: int, project: int,
                     note: str = "") -> bool:
    """The card in place of the message (`edit`) or as a new one; False if the hypothesis is gone."""
    view = await asyncio.to_thread(_card_view, pool.storage.home, number, project)
    if view is None:
        return False
    text, markup = view
    await screens.send(message, edit)(_noted(note, text), parse_mode="HTML", reply_markup=markup)
    return True


def _back_to_card(number: int, project: int) -> list:
    return [button("✖️ Отмена", HypothesisCB(action="card", id=number, project=project))]


@router.callback_query(HypothesisCB.filter(F.action == "card"))
async def card(callback: CallbackQuery, callback_data: HypothesisCB, state: FSMContext, pool: WorkerPool,
               workspace: Workspace):
    await state.clear()  # a ✏️ or a 🏁 left half-way: its step must not take the next message
    if not await _show_card(callback.message, True, pool, callback_data.id, workspace.project_id):
        await callback.answer(GONE, show_alert=True)
        return
    await callback.answer()


@router.callback_query(HypothesisCB.filter(F.action == "close"))
async def close_ask(callback: CallbackQuery, callback_data: HypothesisCB, state: FSMContext, pool: WorkerPool,
                    workspace: Workspace):
    """🏁: its verdict asked in place, then its insight (typed, or none)."""
    hypothesis = outreach.find_hypothesis(pool.storage.home, callback_data.id)
    if hypothesis is None or hypothesis.get("closed"):
        await callback.answer(GONE if hypothesis is None else CLOSED, show_alert=True)
        return
    await callback.answer()
    project = workspace.project_id
    if not callback_data.value:
        rows = [[button(label, HypothesisCB(action="close", id=hypothesis["id"], value=key, project=project))]
                for key, label in outreach.VERDICTS.items()]
        rows.append(_back_to_card(hypothesis["id"], project))
        await callback.message.edit_text(
            f"🏁 Итог гипотезы <b>#{hypothesis['id']}</b>:\n<blockquote>{html.escape(hypothesis['text'])}"
            "</blockquote>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        return
    await state.clear()
    await state.set_state(Hypotheses.insight)
    await state.update_data(hypothesis=hypothesis["id"], verdict=callback_data.value)
    await callback.message.edit_text(
        f"{outreach.VERDICTS[callback_data.value]}. Вывод одной строкой — что узнали и что проверить дальше? "
        "Он попадёт в отчёт.",
        reply_markup=cancel_kb(Hypotheses.insight, choice_kb("hyp_skip", [("Без вывода", "skip")])))


async def _close(message: Message, state: FSMContext, pool: WorkerPool, project: int, insight: str, edit: bool):
    data = await state.get_data()
    await state.clear()
    number = data.get("hypothesis")
    closed = await asyncio.to_thread(outreach.close_hypothesis, pool.storage.home, number, data.get("verdict"),
                                     insight)
    note = (f"🏁 Гипотеза #{number} закрыта." if closed else
            GONE if outreach.find_hypothesis(pool.storage.home, number) is None else CLOSED)
    await _show(message, edit, pool, project, note)


@router.callback_query(Hypotheses.insight, ChoiceCB.filter(F.scope == "hyp_skip"))
async def close_without_insight(callback: CallbackQuery, state: FSMContext, pool: WorkerPool, workspace: Workspace):
    await callback.answer()
    await _close(callback.message, state, pool, workspace.project_id, "", edit=True)


@router.message(Hypotheses.insight)
async def close_with_insight(message: Message, state: FSMContext, pool: WorkerPool, workspace: Workspace):
    insight = await require_text(message)
    if insight is None:
        return
    await _close(message, state, pool, workspace.project_id, insight, edit=False)


@router.callback_query(HypothesisCB.filter(F.action == "edit"))
async def edit_ask(callback: CallbackQuery, callback_data: HypothesisCB, state: FSMContext, pool: WorkerPool):
    """✏️: the new text typed (its verdict and numbers stay)."""
    if outreach.find_hypothesis(pool.storage.home, callback_data.id) is None:
        await callback.answer(GONE, show_alert=True)
        return
    await callback.answer()
    await state.clear()
    await state.set_state(Hypotheses.text)
    await state.update_data(hypothesis=callback_data.id)
    await callback.message.edit_text(f"✏️ Новый текст гипотезы #{callback_data.id} одним сообщением:",
                                     reply_markup=cancel_kb(Hypotheses.text))


@router.message(Hypotheses.text)
async def edit_apply(message: Message, state: FSMContext, pool: WorkerPool, workspace: Workspace):
    text = await require_text(message)
    if text is None:
        return
    number = (await state.get_data()).get("hypothesis")
    await state.clear()
    if not await asyncio.to_thread(outreach.edit_hypothesis, pool.storage.home, number, text):
        await _show(message, False, pool, workspace.project_id, GONE)
        return
    await _show_card(message, False, pool, number, workspace.project_id, "✅ Текст гипотезы изменён.")


@router.callback_query(HypothesisCB.filter(F.action == "delete"))
async def delete(callback: CallbackQuery, callback_data: HypothesisCB, pool: WorkerPool, workspace: Workspace):
    """🗑: asked first; only a hypothesis no mailing ran under (outreach.delete_hypothesis)."""
    home, number, project = pool.storage.home, callback_data.id, workspace.project_id
    if outreach.find_hypothesis(home, number) is None:
        await callback.answer(GONE, show_alert=True)
        return
    if outreach.has_runs(home, number):
        await callback.answer(HAS_RUNS, show_alert=True)
        return
    if callback_data.value != "yes":
        await callback.answer()
        rows = [[button("🗑 Да, удалить", HypothesisCB(action="delete", id=number, value="yes", project=project))],
                _back_to_card(number, project)]
        await callback.message.edit_text(f"🗑 Удалить гипотезу #{number}?",
                                         reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        return
    deleted = await asyncio.to_thread(outreach.delete_hypothesis, home, number)
    await callback.answer()
    await _show(callback.message, True, pool, project, f"🗑 Гипотеза #{number} удалена." if deleted else GONE)


def _earlier(home: str) -> int:
    """How many people the mailing wrote to before the project's first outreach run: not in the logs."""
    first = min((run["started"] for run in outreach.load(home)["runs"]), default=None)
    return projects.mailed(home, first)


def _report(pool: WorkerPool, name: str) -> bytes:
    home = pool.storage.home
    workers = {os.path.basename(path): f"@{username}" for path, username in pool.storage.usernames.items()
               if username}
    return outreach.report_md(home, name, _earlier(home), workers).encode("utf-8")


@router.callback_query(HypothesisCB.filter(F.action == "report"))
async def report(callback: CallbackQuery, pool: WorkerPool, workspace: Workspace):
    """📄: the report as a document under the screen (it stays, as 📦 Скачать базу's)."""
    await callback.answer()
    name = screens.project_name(workspace.project_id) or str(workspace.project_id)
    data = await asyncio.to_thread(_report, pool, name)
    await callback.message.answer_document(
        BufferedInputFile(data, filename=f"{name}_outreach_{datetime.now():%Y-%m-%d}.md"),
        caption=f"📄 Отчёт по гипотезам проекта «{name}»: таблица, тексты, цифры по воркерам, ответы людей.")
