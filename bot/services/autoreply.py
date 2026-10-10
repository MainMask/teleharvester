"""Auto-reply to people who answer a PM mailing.

Bot-mode workers are online only while a job drives them, so nothing listens for
replies between jobs. Instead a background loop polls each worker every
settings.autoreply_interval seconds: it reads the unread private chats, answers each
person the worker wrote to first with settings.autoreply_text (once per worker and
person), marks the chat read and forwards what they wrote to the admins in the bot.
What an answered person writes later is forwarded too, without another reply.
On/off and the text are changed from the bot (bot/routers/autoreply.py) and take
effect on the next round.
"""

import asyncio
import logging
import os
from collections import Counter
from datetime import datetime, timedelta

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from telethon import types

from modules import json_file, rich_message
from modules.storages.sessions_storage import connect_client, release_client

# logging, not the Rich console: under systemd Rich wraps a line at 80 columns into several
# journald entries; bot/__main__.py routes logging to journald one line per event
log = logging.getLogger(__name__)


REPLIED_PATH = os.path.join("stats", "auto_replies.json")
SERVICE_ID = 777000      # Telegram's service account: login codes, never answered
DIALOGS_LIMIT = 100      # one GetDialogs request; fresh replies sit at the top
WORKER_TIMEOUT = 120     # a dead proxy must not stall the whole round
NOTIFY_TEXT_LIMIT = 3000
MAX_FORWARDED = 10       # unread messages forwarded per chat and round
NOT_MAILING_LIMIT = 10_000  # past this many, _not_mailing is dropped: each such chat is searched once more
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
NOTIFY_ATTEMPTS = 3      # a burst of replies can hit the Bot API's flood limit

_task = None
# unread chats found not to be mailing replies, as "path:user_id:last message id": their
# search request isn't repeated every round, only when the person writes again
_not_mailing: set = set()


def load_replied() -> dict:
    try:
        return json_file.load(REPLIED_PATH, {})
    except (OSError, ValueError):  # a broken file: start over rather than stop the loop
        return {}


def save_replied(replied: dict):
    json_file.save(REPLIED_PATH, replied)


def _awaits_reply(dialog) -> bool:
    """A private chat with a live person whose last message is unread and theirs."""
    user = dialog.entity
    if not isinstance(user, types.User):
        return False
    if user.bot or user.is_self or user.deleted or user.id == SERVICE_ID:
        return False
    return dialog.unread_count > 0 and dialog.message is not None and not dialog.message.out


def _describe(user) -> str:
    name = " ".join(part for part in (user.first_name, user.last_name) if part)
    handle = f"@{user.username}" if user.username else f"id {user.id}"
    who = f"{name} ({handle})" if name else handle
    return f"{who}, 📱 +{user.phone}" if user.phone else who


def profile_url(user) -> str:
    return f"https://t.me/{user.username}" if user.username else f"tg://user?id={user.id}"


async def _unread_texts(client, dialog) -> str:
    """The person's unread messages, oldest first ([медиа] for one without text)."""
    messages = await client.get_messages(dialog.entity, limit=min(dialog.unread_count, MAX_FORWARDED))
    texts = [message.message or "[медиа]" for message in reversed(messages) if not message.out]
    if dialog.unread_count > MAX_FORWARDED:
        texts.insert(0, f"…и ещё {dialog.unread_count - MAX_FORWARDED} раньше")
    return "\n".join(texts)[:NOTIFY_TEXT_LIMIT]


async def _handle_dialog(client, dialog, path: str, label: str, text: str, replied: dict, notify) -> int:
    """One unread chat: answer it if it's a fresh mailing reply, forward it; 1 if answered."""
    key = f"{path}:{dialog.entity.id}"
    first = key not in replied

    # only replies to a mailing: the worker wrote to this person first
    if first:
        checked = f"{key}:{dialog.message.id}"
        if checked in _not_mailing:
            return 0
        mine = await client.get_messages(dialog.entity, limit=1, from_user="me")
        if not mine:
            if len(_not_mailing) >= NOT_MAILING_LIMIT:  # a new entry per stranger's message: bounded over weeks
                _not_mailing.clear()
            _not_mailing.add(checked)
            return 0
        if (mine[0].message or "").strip() == text.strip():
            # answered in a round whose connection dropped (a job released the worker) before
            # the reply was recorded: record it now instead of answering twice
            replied[key] = datetime.now().strftime(TIME_FORMAT)
            save_replied(replied)
            first = False

    incoming = await _unread_texts(client, dialog)  # before our reply lands among them

    answered = 0
    header = "💬 Ответ на рассылку — автоответ отправлен" if first else "💬 Новое сообщение (автоответ уже был)"
    if first:
        try:
            await client.send_message(dialog.entity, text, parse_mode=None)  # as typed: no markdown
        except ConnectionError:
            raise
        except Exception as err:  # this person can't be written to (blocked, privacy, a flood limit):
            # the admins still get what they wrote; their next message is another try
            header = f"⚠️ Ответ на рассылку — автоответ НЕ отправлен: {rich_message.refusal_reason(err) or err}"
        else:
            replied[key] = datetime.now().strftime(TIME_FORMAT)
            save_replied(replied)  # before anything else can fail: never answer twice
            answered = 1

    # notify before the read mark: a dropped connection there must not lose it
    delivered = await notify(f"{header}\nОт: {_describe(dialog.entity)}\nВоркер: {label}\n\n{incoming}",
                             profile_url(dialog.entity))
    if not delivered:  # no admin got it (the Bot API is down): left unread, the next round forwards it
        return answered

    await client.send_read_acknowledge(dialog.entity, max_id=dialog.message.id)
    return answered


async def poll_worker(client, path: str, label: str, text: str, replied: dict, notify):
    """Answer the worker's unread mailing replies and forward them (and what answered
    people write later) to the admins; returns how many were answered."""
    answered = 0

    async for dialog in client.iter_dialogs(limit=DIALOGS_LIMIT):
        if not _awaits_reply(dialog):
            continue
        try:
            answered += await _handle_dialog(client, dialog, path, label, text, replied, notify)
        except ConnectionError:
            raise  # the worker itself is gone (e.g. a job disconnected it): its other chats would fail too
        except Exception as err:  # this chat only: the worker's other chats still get their turn
            log.warning("autoreply: %s: chat %s: %s", label, dialog.entity.id, err)

    return answered


async def poll_once(pool, text: str, replied: dict, notify):
    """One round over the workers, one at a time."""
    for client in pool.workers:
        path = pool.storage.get_session_path(client)
        if path is None:  # replaced (new proxies) or removed since the round began: polled next round
            continue
        if pool.scraping is not None and pool.scraping.path == path:
            continue  # the scraper drives this account from its own client

        label = pool.storage.usernames.get(path) or os.path.basename(path or "")
        pool.polling = path  # before any await: a scrape must not start on it meanwhile
        async def connect_and_poll():
            await connect_client(client)  # a no-op if a job already has it connected
            await poll_worker(client, path, label, text, replied, notify)

        try:
            # connect() inside the timeout too, as SessionsStorage.fetch_me: a dead proxy must not stall the round
            await asyncio.wait_for(connect_and_poll(), WORKER_TIMEOUT)
        except Exception as err:  # this worker only: banned, logged out, a job dropped it
            log.warning("autoreply: %s: %s", label, err or type(err).__name__)  # a timeout has no text
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():  # the bot is stopping: stop() / shutdown
                raise
            # a job released this worker mid-request: Telethon cancels a disconnected client's
            # pending requests, which would otherwise end the whole loop silently
            log.warning("autoreply: %s: disconnected mid-poll", label)
        finally:
            pool.polling = None  # before the busy check, or it would see this poll itself
            if not pool.busy(path):  # a job's worker stays connected: the job disconnects it
                await release_client(client)  # the cache would grow every round


def reply_stats(replied: dict, now: datetime) -> dict:
    """How many people were answered: in all, in the last day / week, per worker path."""
    day = week = 0
    per_worker = Counter()
    for key, when in replied.items():
        per_worker[key.rsplit(":", 1)[0]] += 1
        try:
            age = now - datetime.strptime(when, TIME_FORMAT)
        except (TypeError, ValueError):
            continue
        day += age <= timedelta(days=1)
        week += age <= timedelta(days=7)
    return {"total": len(replied), "day": day, "week": week, "per_worker": per_worker}


async def _send(bot, chat_id, message: str, markup=None):
    """send_message, waiting out the Bot API's flood limit a few times."""
    for attempt in range(NOTIFY_ATTEMPTS):
        try:
            return await bot.send_message(chat_id, message, reply_markup=markup)
        except TelegramRetryAfter as err:
            if attempt == NOTIFY_ATTEMPTS - 1:
                raise
            await asyncio.sleep(err.retry_after)


def make_notify(bot, admins):
    async def notify(message: str, url: str) -> bool:
        """True if at least one admin got it."""
        delivered = False
        markup = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="👤 Открыть профиль", url=url)]]
        )
        for admin in admins:
            try:
                try:
                    await _send(bot, admin, message, markup)
                except TelegramBadRequest:  # a tg://user link the person's privacy forbids
                    await _send(bot, admin, message)
                delivered = True
            except Exception as err:
                log.warning("autoreply: notify %s: %s", admin, err)
        return delivered
    return notify


async def _loop(bot, pool, settings, admins):
    replied = load_replied()
    notify = make_notify(bot, admins)

    while True:
        await asyncio.sleep(settings.autoreply_interval)
        if not (settings.autoreply_enabled and settings.autoreply_text):
            continue  # off from the bot or config: checked again next round
        try:
            await poll_once(pool, settings.autoreply_text, replied, notify)
        except Exception as err:  # never let one bad round end the loop
            log.warning("autoreply: round failed: %s", err)


async def start(bot, pool, settings, admins):
    """Bot startup: run the loop in the background; it idles while auto-reply is off."""
    global _task
    # kept referenced, or the loop's weak ref lets GC drop it
    _task = asyncio.create_task(_loop(bot, pool, settings, admins))


async def stop():
    if _task is not None:
        _task.cancel()
