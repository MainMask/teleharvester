"""🌐 Прокси: the project's proxies for its accounts, and the optional ones for its permanently restricted
workers."""
import os

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import ChoiceCB, MenuAction, MenuCB
from bot.keyboards.common import cancel_kb, choice_kb
from bot.keyboards.menu import back_kb
from bot.routers._common import ensure_workers
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager, busy
from bot.states import SetProxy
from functions.spamblock import rehome, rehome_report, set_restricted_proxies
from modules import projects, restricted_workers, tdata_import
from modules.registry import ACCOUNTS, MAIN_MENU
from modules.types.proxy import ACCOUNTS_PER_PROXY, overlap, parse_proxies

router = Router()

MAX_PROXY_FILE_SIZE = 256 * 1024       # a proxy list is tiny; reject anything clearly wrong

SPARE_PROMPT = (
    "Ещё, по желанию: отдельные прокси для <b>бессрочно ограниченных</b> воркеров — они освободят места на "
    "основных и продолжат отвечать на входящие. Хватит одного-двух: пришлите их так же или нажмите ⏭ Пропустить."
)

PROXY_PROMPT = (
    "🌐 Прокси — пришлите список прокси <b>.txt файлом</b> или текстом, по одному на строку:\n"
    "<code>scheme://user:pass@ip:port</code> (socks5/socks4/http).\n\n"
    f"Они распределятся по аккаунтам проекта: один прокси на {ACCOUNTS_PER_PROXY} аккаунта. Новые аккаунты "
    "проекта тоже получат прокси из этого списка."
)


async def proxies_busy(message, pool, manager) -> bool:
    """A proxy change now would rebuild clients in use (a job, the autoreply poll): told, True."""
    if manager.active:  # a job may have started since the prompt or the button
        await message.answer(busy(manager.label))
        return True
    if pool.polling is not None:  # its old client is connected through the old proxy until the poll ends
        await message.answer("⛔ Идёт проверка автоответа. Повторите через минуту.")
        return True
    return False


async def rehome_now(message, pool) -> str | None:
    """Move the restricted workers onto their proxies and the others off the pool back into it now
    (functions.spamblock.rehome), but the ones in use outside the job slot (a scrape, 🔑 Код входа): their
    clients must not change under them, the next status check offers them. The report, or None once a bad
    proxy list is told."""
    busy = {path for path in pool.storage.jsessions_paths if pool.busy(path) or pool.scraped(path)}
    try:
        return rehome_report(rehome(pool.storage, busy=busy))
    except ValueError as err:  # a list edited by hand
        await message.answer(f"⚠️ Пересадка невозможна: {err}. Исправьте — 🌐 Прокси.")
        return None


@router.callback_query(MenuCB.filter(F.action == MenuAction.PROXY))
async def proxy_start(callback: CallbackQuery, state: FSMContext, pool: WorkerPool, manager: JobManager):
    if not await ensure_workers(callback, pool):
        return

    if manager.active:
        await callback.message.answer(busy(manager.label))
        return

    await state.set_state(SetProxy.input)
    await callback.message.answer(PROXY_PROMPT, parse_mode="HTML", reply_markup=cancel_kb(SetProxy.input))


async def _proxy_list(message: Message):
    """The proxies a message holds (a .txt or text) and its raw text; None once the problem is told."""
    if message.document is not None:
        if (message.document.file_size or 0) > MAX_PROXY_FILE_SIZE:
            await message.answer("Файл слишком большой для списка прокси. Пришлите .txt поменьше.")
            return None
        buffer = await message.bot.download(message.document)
        raw = buffer.read().decode("utf-8", "replace")
    else:
        raw = message.text or ""

    try:
        proxies = parse_proxies(raw)
    except ValueError as err:
        await message.answer(f"Не удалось разобрать прокси: {err}. Исправьте и пришлите ещё раз:")
        return None

    if not proxies:
        await message.answer("Пустой список. Пришлите хотя бы один прокси.")
        return None
    return proxies, raw


@router.message(SetProxy.input, ~F.text.in_(MAIN_MENU))
async def proxy_apply(message: Message, state: FSMContext, pool: WorkerPool, manager: JobManager):
    if (got := await _proxy_list(message)) is None:
        return
    proxies, raw = got
    try:  # the restricted workers' own proxies, set before: they stay off this list (see SPARE_PROMPT)
        spare = tdata_import.load_proxies(projects.restricted_proxies_path(pool.storage.home))
    except ValueError as err:  # edited badly: that list first (its step), or the pool's could fall short
        await state.set_state(SetProxy.spare)
        await message.answer(
            f"⚠️ Список прокси для ограниченных испорчен ({err}). Сначала пришлите его заново или нажмите "
            "«🗑 Не отделять», затем снова 🌐 Прокси с основным списком.",
            reply_markup=cancel_kb(SetProxy.spare, choice_kb("proxy_spare", [("🗑 Не отделять", "clear")])))
        return
    if overlap(proxies, spare):
        await message.answer("⚠️ В списке есть прокси для бессрочно ограниченных. Уберите их — или сначала "
                             "отключите отделение: 🌐 Прокси без них, затем «🗑 Не отделять».")
        return
    if await proxies_busy(message, pool, manager):
        return

    try:
        summary = pool.storage.apply_proxies(proxies, spare, set(restricted_workers.load()))
    except ValueError as err:
        await message.answer(f"⚠️ {err}. Пришлите больше прокси.")
        return

    tdata_import.save_proxies(raw, projects.proxies_path(pool.storage.home))

    await state.set_state(SetProxy.spare)

    text = (
        f"✅ Прокси применены к <b>{summary['accounts']}</b> аккаунтам "
        f"(использовано прокси: {summary['proxies_used']})."
    )
    if summary["restricted"]:
        text += f"\nБессрочно ограниченных на своих прокси: {summary['restricted']}."
    if summary["string_sessions_skipped"]:
        text += f"\n⚠️ Пропущено .session-аккаунтов (без метаданных): {summary['string_sessions_skipped']}."

    # the result and the optional next step in one message; 🗑 for a list there, readable or not: a broken
    # one can be dropped too
    aside = os.path.exists(projects.restricted_proxies_path(pool.storage.home))
    buttons = [("⏭ Пропустить", "skip")] + ([("🗑 Не отделять", "clear")] if aside else [])
    now = f" Сейчас задано: {len(spare)}." if spare else ""
    await message.answer(f"{text}\n\n{SPARE_PROMPT}{now}", parse_mode="HTML",
                         reply_markup=cancel_kb(SetProxy.spare, choice_kb("proxy_spare", buttons)))


@router.callback_query(SetProxy.spare, ChoiceCB.filter(F.scope == "proxy_spare"))
async def proxy_spare_choice(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext,
                             pool: WorkerPool, manager: JobManager):
    await callback.answer()
    if callback_data.value == "skip":
        await state.clear()
        await callback.message.edit_text("✅ Прокси для ограниченных — без изменений.",  # in the question's place
                                         reply_markup=back_kb(ACCOUNTS, manager.project_id, fresh=False))
        return

    # 🗑 Не отделять: the list goes, the ones on it back into the pool
    if await proxies_busy(callback.message, pool, manager):
        return
    set_restricted_proxies(pool.storage, None)
    await state.clear()
    report = await rehome_now(callback.message, pool)
    await callback.message.edit_text("🗑 Ограниченные больше не отделяются." + (f"\n{report}" if report else ""),
                                     reply_markup=back_kb(ACCOUNTS, manager.project_id, fresh=False))


@router.message(SetProxy.spare, ~F.text.in_(MAIN_MENU))
async def proxy_spare_apply(message: Message, state: FSMContext, pool: WorkerPool, manager: JobManager):
    if (got := await _proxy_list(message)) is None:
        return
    spare, raw = got
    try:
        main = tdata_import.load_proxies(projects.proxies_path(pool.storage.home))
    except ValueError as err:
        await message.answer(f"⚠️ Основной список прокси испорчен ({err}): пришлите его заново — 🌐 Прокси.")
        return
    if overlap(spare, main):
        await message.answer("⚠️ Эти прокси уже в основном списке: для ограниченных нужны другие.")
        return
    if await proxies_busy(message, pool, manager):
        return
    set_restricted_proxies(pool.storage, raw)
    await state.clear()
    report = await rehome_now(message, pool)
    await message.answer("✅ Прокси для ограниченных сохранены." + (f"\n{report}" if report else ""),
                         reply_markup=back_kb(ACCOUNTS, manager.project_id))
