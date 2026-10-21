"""🤖 Аккаунты' screen and its 📋 Список: the project's workers, polled, then the personal accounts (card.py)."""
import asyncio
import html

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import MenuAction, MenuCB
from bot.keyboards.menu import back_kb
from bot.routers._common import send_chunked
from bot.routers.accounts import card
from bot.services import screens
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from modules import phone_countries, restricted_workers
from modules.registry import ACCOUNTS, menu_path
from modules.storages.sessions_storage import profile_order

router = Router()

GET_ME_TIMEOUT = 20  # seconds per worker when polling profiles for the accounts list

NO_WORKERS = (f"Добавьте аккаунт ({menu_path('add')}) — по номеру, из tdata или из свободных, — "
              "чтобы запускать задачи проекта.")


def _worker_line(storage, client, me, index: int, busy: bool = False, mark: str = "") -> str:
    """One account's card for the list; falls back to stored data when get_me() failed
    or was not asked (busy: the scraper runs on it)."""
    if me is None:
        status = "⏳ занят скрапом" if busy else "⚠️ не удалось опросить"
        js = storage.jsessions_paths.get(storage.get_session_path(client))
        if js is not None:
            account = js.account.account
            name = " ".join(filter(None, [account.first_name, account.last_name])) or "—"
            return (f"<b>{index}. {html.escape(name)}</b>\n"
                    f"📱 <code>+{html.escape(str(account.phone_number))}</code> · "
                    f"🆔 <code>{account.user_id}</code>\n"
                    f"{status}{mark}")
        return f"<b>{index}.</b> {status} (StringSession){mark}"

    name = " ".join(filter(None, [me.first_name, me.last_name])) or "—"
    username = f"@{html.escape(me.username)}" if me.username else "—"  # plain: Telegram links it
    premium = " · ⭐ Premium" if me.premium else ""
    return (f"<b>{index}. {html.escape(name)}</b>\n"
            f"👤 {username} · 🆔 <code>{me.id}</code>{premium}{mark}")


@screens.view(ACCOUNTS)
async def accounts_screen(message: Message, state: FSMContext, edit: bool, pool: WorkerPool, **_):
    header = f"🤖 <b>Аккаунты проекта</b> — воркеров: <b>{pool.count()}</b>"
    if pool.count() == 0:
        header += f"\n\n{NO_WORKERS}"
    await screens.show(message, ACCOUNTS, edit, header)


@router.callback_query(MenuCB.filter(F.action == MenuAction.LIST))
async def list_accounts(callback: CallbackQuery, state: FSMContext, pool: WorkerPool, manager: JobManager, personal):
    await callback.answer()
    await accounts(callback.message, state, pool, manager, personal)


async def accounts(message: Message, state: FSMContext, pool: WorkerPool, manager: JobManager, personal):
    order = await _list_workers(message, pool, manager)
    await card.list_personal(message, state, pool, personal, order, manager.project_id)


def _countries(storage, workers, profiles) -> str:
    """The workers' countries, a line under the list's header; "" with no number known."""
    countries = phone_countries.summary(phone_countries.phone_of(storage, client, me)
                                        for client, me in zip(workers, profiles))
    return f"\n🌍 {html.escape(countries)}" if countries else ""


async def _list_workers(message: Message, pool: WorkerPool, manager: JobManager) -> list[str]:
    """The workers' list; their session paths in its order ([] when it wasn't polled)."""
    count = pool.count()

    if count == 0:
        await message.answer(
            "🤖 <b>Аккаунты</b>\n\n"
            f"🤖 Воркеров: <b>0</b>\n\n{NO_WORKERS}",
            parse_mode="HTML", reply_markup=back_kb(ACCOUNTS, manager.project_id),
        )
        return []

    if not manager.acquire("Просмотр аккаунтов", cancelable=False, timeout=0):
        workers = pool.workers  # their stored numbers only: none is polled now
        await message.answer(
            f"🤖 <b>Аккаунты</b> — всего: <b>{count}</b>{_countries(pool.storage, workers, [None] * len(workers))}"
            f"\n\n⛔ Идёт задача «{manager.label}». Детали аккаунтов — после окончания.",
            parse_mode="HTML",
        )
        return []

    # the scraper's worker isn't polled: its auth key is in use by the scrape's own client, and
    # a second connection from another IP (a rotating proxy) risks AUTH_KEY_DUPLICATED
    scraping = pool.scraping.path if pool.scraping is not None else None

    def busy(client) -> bool:
        return scraping is not None and pool.storage.get_session_path(client) == scraping

    async def profile(client):
        return None if busy(client) else await pool.storage.fetch_me(client, GET_ME_TIMEOUT)

    try:
        workers = pool.workers
        pool.in_job = [w for w in workers if not busy(w)]  # polled now: no scrape starts on them
        profiles = await asyncio.gather(*[profile(client) for client in workers])
    finally:
        pool.in_job = []
        manager.release()

    forever, status = set(restricted_workers.load()), restricted_workers.load_status()
    # the last @SpamBot check: (group, label); restricted ones go last
    checks = {id(client): restricted_workers.classify(pool.storage.get_session_path(client), forever, status)
              for client in workers}

    ordered = sorted(zip(workers, profiles), key=lambda pair: (checks[id(pair[0])][0], profile_order(pair[1])))
    lines = [_worker_line(pool.storage, client, me, i + 1, busy(client), "\n" + html.escape(checks[id(client)][1]))
             for i, (client, me) in enumerate(ordered)]

    header = f"🤖 <b>Аккаунты</b> — всего: <b>{count}</b>"
    if restricted := sum(1 for group, _ in checks.values() if group):
        header += f" · ограничены: <b>{restricted}</b>"
    header += _countries(pool.storage, workers, profiles)
    await send_chunked(message, header, lines)
    return [pool.storage.get_session_path(client) for client, _ in ordered]
