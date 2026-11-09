"""🔥 Прогрев (its views and buttons): the project's warmup — its state and settings, ▶️, its reset, the steps
(🧩 Шаги). The warmup itself: functions/warmup.py; its data: modules/warmup.py; its daily run:
bot/services/warmup_daily.py. The accounts by category: 🤖 Аккаунты (accounts/base.py)."""
import datetime
import html

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.callbacks import FunctionCB, WarmupCB
from bot.keyboards.common import button
from bot.keyboards.menu import back_kb, nav_button, screen_kb
from bot.keyboards.common import cancel_kb
from bot.routers._common import busy_tap, confirm_expired, ensure_workers, require_text, resolve
from bot.services import screens, warmup_daily
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from bot.services.workspaces import Workspace
from bot.states import SetWarmupThreads
from modules import content, projects, warmup
from modules.registry import MANAGE, WARMUP, WARMUP_STEPS, title

router = Router()

NOTHING_DUE = "Некого прогревать сегодня: все аккаунты прогреты, уже прогревались сегодня или ограничены."
MODES = {warmup.OFF: "⬜", warmup.ON: "✅", warmup.KEEP: "🛡"}
# 🔥 Прогрев's own buttons, each with what it does (screens.screen_text's `own`)
OWN_BUTTONS = (("📅 Дней", "сколько дней греть аккаунт, раз в день; прогрет — когда пройдены все"),
               ("🧵 Потоков", "сколько аккаунтов греются одновременно — с одного прокси всегда по одному"),
               ("🔁 Ежедневно", "автозапуск раз в день в случайное время"),
               ("♻️ Сбросить прогрев", "все аккаунты проекта начнут прогрев с первого дня"))


def _button(text: str, action: str, value: str, project: int) -> InlineKeyboardButton:
    return button(text, WarmupCB(action=action, value=value, project=project))


def _markup(rows: list) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


# --- 🔥 Прогрев ---

def _state_line(manager: JobManager, settings: dict, project: int) -> str:
    """Whether it runs now (the project's job slot), and when the daily run is."""
    if not manager.active:
        now = "⏸ Прогрев не идёт"
    elif manager.label == title("warmup"):
        now = "▶️ Идёт прогрев"
    else:
        now = f"⏳ Слот проекта занят: «{html.escape(manager.label)}»"
    today = datetime.datetime.now()
    if not settings["daily"]:
        daily = "выкл"
    elif settings["auto_day"] == today.date().isoformat():
        daily = "сегодня уже был"
    elif today.hour >= warmup_daily.LAST_HOUR:  # past its window: it waits for tomorrow
        daily = "завтра"
    else:
        daily = f"сегодня ~{warmup_daily.planned(project, today):%H:%M}"
    return f"{now} · 🔁 Автозапуск: {daily}"


def _warmup_view(pool: WorkerPool, manager: JobManager, project: int) -> tuple[str, InlineKeyboardMarkup]:
    settings = warmup.load_settings(pool.storage.home)
    header = "\n".join([
        screens.title_line(WARMUP, screens.project_name(project)),
        "",
        _state_line(manager, settings, project),
        warmup.summary(pool.storage) or "Аккаунтов нет.",
        f"{html.escape(warmup.region_title(pool.storage.home))} — меняется в «{MANAGE}»",
    ])
    # its settings under ▶️ Прогреть, before 🧩 Шаги › (screen_kb's `own`)
    own = [
        [_button(f"📅 Дней: {settings['days']}", "set", "days", project),
         _button(f"🧵 Потоков: {settings['threads']}", "threads", "", project)],
        [_button(f"🔁 Ежедневно: {'вкл' if settings['daily'] else 'выкл'}", "set", "daily", project)],
        [_button("♻️ Сбросить прогрев", "reset", "", project)],
    ]
    return screens.screen_text(WARMUP, header, own=OWN_BUTTONS), screen_kb(WARMUP, project, own=own)


@screens.view(WARMUP)
async def warmup_screen(message: Message, state: FSMContext, edit: bool, pool: WorkerPool, manager: JobManager,
                        workspace: Workspace, **_):
    text, markup = _warmup_view(pool, manager, workspace.project_id)
    await screens.send(message, edit)(text, parse_mode="HTML", reply_markup=markup)


@router.callback_query(FunctionCB.filter(F.key == "warmup"))
async def warmup_run(callback: CallbackQuery, pool: WorkerPool, functions: dict, manager: JobManager):
    # not the one the scraper runs on: the warmup runs without it (WorkerPool.delegate)
    paths = [path for path in pool.storage.full_sessions if not pool.scraped(path)]
    if pool.count() and not warmup.due(pool.storage, paths):
        await callback.answer(NOTHING_DUE, show_alert=True)
        return
    if not await ensure_workers(callback, pool):
        return
    instance, bot_function = resolve(functions, "warmup")
    await manager.run(callback.bot, callback.message.chat.id, pool, instance, bot_function,
                      lambda f, r: f.run(r), "Прогрев…")


@router.callback_query(WarmupCB.filter(F.action == "threads"))
async def threads_ask(callback: CallbackQuery, state: FSMContext, pool: WorkerPool):
    """🧵: the number typed, a prompt as a new message (🔥's screen stays)."""
    await callback.answer()
    await state.clear()  # an abandoned flow's data must not reach this one
    await state.set_state(SetWarmupThreads.value)
    threads = warmup.load_settings(pool.storage.home)["threads"]
    await callback.message.answer(
        f"🧵 Потоков — сейчас <b>{threads}</b>. Пришлите, сколько аккаунтов греть одновременно (целое число от 1).\n\n"
        "С одного прокси аккаунты всегда греются по одному, так что одновременно греется не больше, чем прокси у "
        f"аккаунтов проекта (сейчас <b>{warmup.proxy_count(pool.storage)}</b>): больше потоков не ускорит прогрев.",
        parse_mode="HTML", reply_markup=cancel_kb(SetWarmupThreads.value))


@router.message(SetWarmupThreads.value)
async def threads_typed(message: Message, state: FSMContext, pool: WorkerPool, manager: JobManager,
                        workspace: Workspace):
    raw = await require_text(message)
    if raw is None:
        return
    if not raw.isdecimal() or int(raw) < 1:
        await message.answer("Нужно целое число от 1. Пришлите ещё раз:")
        return
    settings = warmup.load_settings(pool.storage.home)
    settings["threads"] = int(raw)
    warmup.save_settings(pool.storage.home, settings)
    await state.clear()
    # 🔥 Прогрев again, with the new value: the one above still shows the old
    text, markup = _warmup_view(pool, manager, workspace.project_id)
    await message.answer(f"✅ Потоков: {int(raw)}. Действует со следующего запуска прогрева.\n\n{text}",
                         parse_mode="HTML", reply_markup=markup)


@router.callback_query(WarmupCB.filter(F.action == "reset"))
async def reset_ask(callback: CallbackQuery, workspace: Workspace):
    await callback.answer()
    project = workspace.project_id
    await callback.message.edit_text(  # in 🔥's place: ✖️ brings it back
        "♻️ Сбросить прогрев всех аккаунтов проекта? Они начнут прогрев с первого дня (оформление — заново).",
        reply_markup=_markup([[_button("♻️ Да, сбросить", "resetok", "", project),
                               nav_button("✖️ Отмена", WARMUP, project)]]))


@router.callback_query(WarmupCB.filter(F.action == "resetok"))
async def reset_run(callback: CallbackQuery, pool: WorkerPool, manager: JobManager, workspace: Workspace):
    if confirm_expired(callback.message):
        await callback.answer("Подтверждение устарело — нажмите «♻️ Сбросить прогрев» заново.", show_alert=True)
        return
    if await busy_tap(callback, manager):  # a running warmup would write its day over the reset
        return
    await callback.answer()
    paths = projects.account_files(pool.storage.directory)
    warmup.forget([warmup.key(pool.storage, path) for path in paths])
    await callback.message.edit_text(f"✅ Прогрев сброшен: аккаунтов {len(paths)} начнут с первого дня.",
                                     reply_markup=back_kb(WARMUP, workspace.project_id, fresh=False))


# --- 🧩 Шаги ---

def _random_label(field: str, value) -> str:
    if field == "avatars":
        return f"🎲 {value.replace('-', '–')} фото"
    if field == "channels_count":
        return f"🎲 {value.replace('-', '–')} в день"
    return f"🎲 {value}%"


def _steps_view(pool: WorkerPool, project: int) -> tuple[str, InlineKeyboardMarkup]:
    settings = warmup.load_settings(pool.storage.home)
    lines = [
        screens.title_line(WARMUP_STEPS, screens.project_name(project)),
        "",
        "Нажатие переключает шаг: ⬜ выключен · ✅ включён · 🛡 только если поле пустое (заполненное не "
        "перезаписывается). 🎲 рядом — сколько или с каким шансом. Оформление и безопасность — в первый день, "
        "не вышедшее — на следующий.",
    ]
    for group, steps in warmup.GROUPS:
        lines += ["", f"<i>{group}</i>",
                  *(f"<b>{step_title}</b> — {html.escape(hint)}" for _, step_title, hint in steps)]
    lines += ["", f"Списки и файлы — в {html.escape(content.path(settings['region'], ''))} (файлы музыки: "
              "«Исполнитель - Название.mp3»)."]
    rows = []
    for step, step_title in warmup.STEPS.items():
        row = [_button(f"{MODES[settings['steps'][step]]} {step_title}", "set", step, project)]
        if field := warmup.RANDOM.get(step):
            row.append(_button(_random_label(field, settings[field]), "set", field, project))
        rows.append(row)
    return "\n".join(lines), screen_kb(WARMUP_STEPS, project, own=rows)


@screens.view(WARMUP_STEPS)
async def steps_screen(message: Message, state: FSMContext, edit: bool, pool: WorkerPool, workspace: Workspace, **_):
    text, markup = _steps_view(pool, workspace.project_id)
    await screens.send(message, edit)(text, parse_mode="HTML", reply_markup=markup)


@router.callback_query(WarmupCB.filter(F.action == "set"))
async def setting_next(callback: CallbackQuery, callback_data: WarmupCB, pool: WorkerPool, manager: JobManager,
                       workspace: Workspace):
    field = callback_data.value
    if field not in warmup.STEPS and field not in warmup.CYCLES:
        await callback.answer()
        return
    settings = warmup.cycle(warmup.load_settings(pool.storage.home), field)
    if field == "daily" and settings["daily"]:
        settings["chat"] = callback.message.chat.id  # the daily run reports here
    warmup.save_settings(pool.storage.home, settings)
    await callback.answer()
    if field in warmup.STEPS or field in warmup.RANDOM.values():
        text, markup = _steps_view(pool, workspace.project_id)
    else:
        text, markup = _warmup_view(pool, manager, workspace.project_id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
