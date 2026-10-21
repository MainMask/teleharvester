"""The personal accounts (personal_sessions/) under 📋 Список, and each account's card: its login codes, a
personal one's removal."""
import asyncio
import html
import secrets
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from bot.callbacks import ChoiceCB
from bot.keyboards.common import choice_kb, page_of, paged_kb
from bot.keyboards.menu import back_kb
from bot.services import login as logins
from bot.services.delegation import WorkerPool
from modules import login as sign_in, scraped_files
from modules.registry import ACCOUNTS, menu_path
from modules.scraper_creds import card_lines, find_account, scrape_accounts

router = Router()

STALE_LIST = f"Список устарел — откройте заново: {menu_path('list')}."


async def list_personal(message: Message, state: FSMContext, pool: WorkerPool, personal, order=(), project: int = 0):
    """The personal accounts, as stored: one is never connected just to be listed; under them a
    button for each account's card, as listed: the workers in `order` (their list's), then the personal ones;
    ⬅️ to `project`'s 🤖 Аккаунты."""
    rank = {path: i for i, path in enumerate(order)}
    listed = sorted(scrape_accounts(pool.storage, personal),
                    key=lambda account: (account.personal, rank.get(account.path, len(rank))))
    if not listed:
        return
    # the buttons carry an index into acc_paths and this list's token: an older list's (or card's)
    # must not act on the account at its index now
    token = secrets.token_hex(4)
    labels = [f"{'👤' if account.personal else '🤖'} {account.label}" for account in listed]
    text = "🗂 Карточка аккаунта — код входа, удаление личного:"
    if own := [account for account in listed if account.personal]:
        lines = "\n".join(f"{i}. {html.escape(account.label)}" for i, account in enumerate(own, 1))
        text = f"👤 <b>Личные</b> (только для скрапа): <b>{len(own)}</b>\n\n{lines}\n\n{text}"
    # a card opens in this message's place, its ⬅️ brings the list back (acc_text)
    await state.update_data(acc_paths=[account.path for account in listed], acc_labels=labels,
                            acc_token=token, acc_text=text, acc_project=project)
    await message.answer(text, parse_mode="HTML", reply_markup=_cards_kb(labels, token, 0, project))


def _cards_kb(labels: list[str], token: str, page: int, project: int):
    """The list's buttons, a page of them (keyboards.paged_kb), each with the list's token; ⬅️ to `project`'s
    🤖 Аккаунты in its place: the list is 📋 Список' last message."""
    markup = paged_kb("acc", [(label, f"{i}.{token}") for i, label in enumerate(labels)], page, f".{token}")
    markup.inline_keyboard.append(back_kb(ACCOUNTS, project, fresh=False).inline_keyboard[0])
    return markup


async def _account_at(state: FSMContext, pool: WorkerPool, personal, key: str):
    """The account of a button's key ("index.token"); None for a stale one (another list since, or a flow
    cleared it)."""
    index, token = key.split(".")
    data = await state.get_data()
    paths, index = data.get("acc_paths", []), int(index)
    if token != data.get("acc_token") or not 0 <= index < len(paths):
        return None
    return find_account(scrape_accounts(pool.storage, personal), paths[index])


def _card(pool: WorkerPool, personal, account, key: str):
    """(text, keyboard) of an account's card: from what is stored, at once — never connected for it."""
    storage = personal if account.personal else pool.storage
    lines = card_lines(storage, account, html=True)
    lines[0] = f"<b>{lines[0]}</b>"
    if pool.scraped(account.path):
        lines.append("⏳ занят скрапом")
    actions = [("🔑 Код входа", f"codes.{key}")]
    if account.personal:
        actions.append(("🗑 Убрать", f"rm.{key}"))
    actions.append(("⬅️ К списку", f"list.{key}"))
    return "\n".join(lines), choice_kb("acc_act", actions, widths=(len(actions) - 1, 1))


def _codes_text(account, codes, now: datetime) -> str:
    lines = [f"🔑 <b>Коды входа</b> — {html.escape(account.label)}", ""]
    if not codes:
        lines.append(f"{sign_in.NO_CODES} и нажмите 🔄 Обновить.")
    for code, sent_at in codes:
        lines.append(f"<code>{sign_in.spaced(code)}</code> · {sign_in.ago(sent_at, now)}")
    return "\n".join(lines)


async def _codes(pool: WorkerPool, account, key: str) -> tuple[str, object]:
    """(text, keyboard) of an account's (a worker's or a personal one's) recent login codes, in its card's place."""
    keyboard = choice_kb("acc_act", [("🔄 Обновить", f"codes.{key}"), ("⬅️ Назад", f"card.{key}")], widths=(2,))
    if pool.scraped(account.path):
        # its key is in use by the scrape's own client: a second connection risks AUTH_KEY_DUPLICATED
        return f"⏳ {html.escape(account.label)} занят скрапом — коды после его окончания.", keyboard
    try:
        codes = await logins.fetch_codes(pool, account.client, account.path)
    except Exception as err:  # a dead proxy, a logged-out session: a timeout has no text
        why = html.escape(str(err) or type(err).__name__)
        return f"⚠️ {html.escape(account.label)}: не удалось подключиться ({why}).", keyboard
    except asyncio.CancelledError:
        if asyncio.current_task().cancelling():  # the bot is stopping
            raise
        # a job's end released this account mid-read: Telethon cancels a disconnected client's requests
        return f"⚠️ {html.escape(account.label)}: соединение прервано — нажмите 🔄.", keyboard
    return _codes_text(account, codes, datetime.now(timezone.utc)), keyboard


@router.callback_query(ChoiceCB.filter(F.scope == "acc"))
async def account_card(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext, pool: WorkerPool,
                       personal):
    if (page := page_of(callback_data.value)) is not None:  # ◀️ · 📄 · ▶️: another page of the list's buttons
        token = callback_data.value.rpartition(".")[2]
        data = await state.get_data()
        if token != data.get("acc_token"):
            await callback.answer(STALE_LIST, show_alert=True)
            return
        await callback.answer()
        await callback.message.edit_reply_markup(reply_markup=_cards_kb(data["acc_labels"], token, page,
                                                                        data.get("acc_project", 0)))
        return
    account = await _account_at(state, pool, personal, callback_data.value)
    if account is None:
        await callback.answer(STALE_LIST, show_alert=True)
        return
    await callback.answer()
    text, markup = _card(pool, personal, account, callback_data.value)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)  # in the list's place


@router.callback_query(ChoiceCB.filter(F.scope == "acc_act"))
async def account_action(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext, pool: WorkerPool,
                         personal):
    """A card's button, in the card's place: 🔑 (and its 🔄, its ⬅️), 🗑 asks first, ⬅️ the list."""
    action, key = callback_data.value.split(".", 1)
    account = await _account_at(state, pool, personal, key)
    if account is None or (action == "rm" and not account.personal):
        await callback.answer(STALE_LIST, show_alert=True)
        return
    await callback.answer()
    if action == "rm":
        await _confirm_removal(callback.message, state, account, key)
        return
    if action == "list":
        data = await state.get_data()
        await callback.message.edit_text(data["acc_text"], parse_mode="HTML",
                                         reply_markup=_cards_kb(data["acc_labels"], data["acc_token"], 0,
                                                                data.get("acc_project", 0)))
        return
    text, markup = _card(pool, personal, account, key) if action == "card" else await _codes(pool, account, key)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)  # 🔄 with nothing new: SafeEdit


async def _confirm_removal(message: Message, state: FSMContext, account, key: str):
    # the button carries the card's key; prm_confirm, this account: it removes this account only
    await state.update_data(prm_confirm=account.path)
    await message.edit_text(  # in the card's place: ↩️ brings it back
        f"Убрать личный аккаунт {html.escape(account.label)}?\n\n"
        f"Бот завершит авторизацию из этого файла и удалит его: <code>{html.escape(account.path)}</code>. "
        "Вход на телефоне не затрагивается; но если файл получен из tdata Telegram Desktop, "
        "тот Desktop тоже выйдет из аккаунта.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            *choice_kb("prm_ok", [("🗑 Да, убрать", key)]).inline_keyboard,
            *choice_kb("acc_act", [("↩️ Отмена", f"card.{key}")]).inline_keyboard]),
    )


def _removal_blocked(pool: WorkerPool, path: str) -> str | None:
    """Why the personal account can't be removed now, or None."""
    if pool.scraped(path):
        return "⛔ На этом аккаунте идёт скрап — уберите его после окончания."
    if pool.busy(path):
        return ("⛔ Бот сейчас читает коды входа или проверяет названия чатов на этом аккаунте — "
                "повторите через несколько секунд.")
    if scraped_files.unfinished_scrape(path):
        return "⛔ На этом аккаунте есть незавершённый скрап — продолжить его можно только на нём."
    return None



@router.callback_query(ChoiceCB.filter(F.scope == "prm_ok"))
async def personal_remove(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext,
                          pool: WorkerPool, personal):
    data = await state.get_data()
    account = await _account_at(state, pool, personal, callback_data.value)
    if account is None or not account.personal or account.path != data.get("prm_confirm"):
        await callback.answer(STALE_LIST, show_alert=True)
        return
    if reason := _removal_blocked(pool, account.path):  # over the confirmation, which stays to tap again
        await callback.answer(reason, show_alert=True)
        return
    await callback.answer()

    await state.clear()
    logged_out = await sign_in.remove_personal(personal, account)
    label = html.escape(account.label)
    await callback.message.edit_text(
        f"✅ Личный аккаунт {label} убран: сессия бота завершена, файл удалён." if logged_out else
        f"⚠️ Файл личного аккаунта {label} удалён, но завершить сессию бота не удалось — "
        "завершите её вручную: Telegram → Настройки → Устройства.",
        reply_markup=back_kb(ACCOUNTS, data.get("acc_project", 0), fresh=False),
    )
