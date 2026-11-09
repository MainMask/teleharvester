"""📋 Список' 📡 Опросить: the project's workers, polled (their cards: 📋 Список' categories,
accounts/base.py)."""
import asyncio
import html

from aiogram.types import Message

from bot.keyboards.menu import back_kb
from bot.routers._common import NO_WORKERS, send_chunked
from bot.services.delegation import WorkerPool
from bot.services import jobs
from bot.services.jobs import JobManager
from modules import phone_countries, restricted_workers
from modules.registry import ACCOUNT_LIST, POLL
from modules.storages.sessions_storage import profile_order
from modules.types.account import full_name

GET_ME_TIMEOUT = 20  # seconds per worker when polling profiles for the accounts list

def _worker_line(storage, client, me, index: int, busy: bool = False, mark: str = "") -> str:
    """One account's card for the list; falls back to stored data when get_me() failed
    or was not asked (busy: the scraper runs on it)."""
    if me is None:
        status = "⏳ занят скрапом" if busy else "⚠️ не удалось опросить"
        js = storage.jsessions_paths.get(storage.get_session_path(client))
        if js is not None:
            account = js.account.account
            name = full_name(account.first_name, account.last_name) or "—"
            return (f"<b>{index}. {html.escape(name)}</b>\n"
                    f"📱 <code>+{html.escape(str(account.phone_number))}</code> · "
                    f"🆔 <code>{account.user_id}</code>\n"
                    f"{status}{mark}")
        return f"<b>{index}.</b> {status} (StringSession){mark}"

    name = full_name(me.first_name, me.last_name) or "—"
    username = f"@{html.escape(me.username)}" if me.username else "—"  # plain: Telegram links it
    premium = " · ⭐ Premium" if me.premium else ""
    return (f"<b>{index}. {html.escape(name)}</b>\n"
            f"👤 {username} · 🆔 <code>{me.id}</code>{premium}{mark}")


def _list_header(count: int) -> str:
    """The poll's first line (HTML): how many workers the project has."""
    return f"<b>{html.escape(POLL)}</b> — воркеров: <b>{count}</b>"


def _countries(storage, workers, profiles) -> str:
    """The workers' countries, a line under the list's header; "" with no number known."""
    countries = phone_countries.summary(phone_countries.phone_of(storage, client, me)
                                        for client, me in zip(workers, profiles))
    return f"\n🌍 {html.escape(countries)}" if countries else ""


async def accounts(message: Message, pool: WorkerPool, manager: JobManager):
    """Every worker asked for its profile, as new messages: the restricted last; ⬅️ to 📋 Список under them."""
    count = pool.count()

    if count == 0:
        await message.answer(f"{_list_header(0)}\n\n{html.escape(NO_WORKERS)}", parse_mode="HTML",
                             reply_markup=back_kb(ACCOUNT_LIST, manager.project_id))
        return

    if not manager.acquire(POLL, cancelable=False, timeout=0):
        workers = pool.workers  # their stored numbers only: none is polled now
        await message.answer(
            f"{_list_header(count)}{_countries(pool.storage, workers, [None] * len(workers))}\n\n"
            f"{html.escape(jobs.busy(manager.label))}\nДетали воркеров — после её окончания.",
            parse_mode="HTML", reply_markup=back_kb(ACCOUNT_LIST, manager.project_id),
        )
        return

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

    header = _list_header(count)
    if restricted := sum(1 for group, _ in checks.values() if group):
        header += f" · ограничены: <b>{restricted}</b>"
    header += _countries(pool.storage, workers, profiles)
    await send_chunked(message, header, lines, reply_markup=back_kb(ACCOUNT_LIST, manager.project_id))
