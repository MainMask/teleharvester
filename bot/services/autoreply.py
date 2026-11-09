"""Replies to a PM mailing: tracked, forwarded, auto-answered if asked.

Bot-mode workers are online only while a job drives them, so nothing listens for
replies between jobs. Instead a background loop polls each worker on a timer of its own, a random
time within settings.autoreply_interval ([min, max] seconds) each time — so no worker keeps a fixed
period and a project's workers never come in a pack, one after another: it reads the unread private chats, and for each
person the worker wrote to first it records the first answer (the ledger, and the
project's outreach log with its text: modules/outreach.py), forwards what they wrote to
the admins in the bot — with ✍️ Ответить, an answer from that worker (bot/routers/replies.py) —
and marks the chat read; with the auto-reply on, it answers them with its project's text (once
per worker and person). What a person writes later is forwarded too, without another reply.
Every message both ways goes to the outreach log, the dialog the report shows.
A project is polled once it has mailed anyone, or while its auto-reply is on.
Each project has its own on/off and text (project_settings: config.toml's [autoreply] for a project
that has set none), changed from the bot (bot/routers/autoreply.py); they take effect on the next round.
"""

import asyncio
import html
import logging
import os
import random
import time
from collections import Counter
from datetime import datetime, timedelta

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from telethon import types

from bot.callbacks import ReplyCB
from functions.base.base import pick_seconds
from modules import json_file, outreach, rich_message
from modules.projects import MAILING_STATS
from modules.storages.sessions_storage import connect_client, release_client
from modules.types.account import full_name

# logging, not the Rich console: under systemd Rich wraps a line at 80 columns into several
# journald entries; bot/__main__.py routes logging to journald one line per event
log = logging.getLogger(__name__)


REPLIED_PATH = os.path.join("stats", "auto_replies.json")
SETTINGS_NAME = "autoreply.json"  # a project's own on/off and text, in its folder
SERVICE_ID = 777000      # Telegram's service account: login codes, never answered
DIALOGS_LIMIT = 100      # one GetDialogs request; fresh replies sit at the top
WORKER_TIMEOUT = 120     # a dead proxy must not stall the whole round
TICK = 60                # at most between the loop's looks: a project turned on, a worker added is seen within it
NOTIFY_TEXT_LIMIT = 3000
MAX_FORWARDED = 10       # unread messages forwarded per chat and round
NOT_MAILING_LIMIT = 10_000  # past this many, _not_mailing is dropped: each such chat is searched once more
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
NOTIFY_ATTEMPTS = 3      # a burst of replies can hit the Bot API's flood limit

_task = None
_replied: dict | None = None  # the ledger the running loop holds (rename_worker edits it in place)
# unread chats found not to be mailing replies, as "path:user_id:last message id": their
# search request isn't repeated every round, only when the person writes again
_not_mailing: set = set()


def project_settings(home: str, settings) -> dict:
    """The project's {"enabled", "text"}: its own once set from the bot, else config.toml's [autoreply]."""
    default = {"enabled": settings.autoreply_enabled, "text": settings.autoreply_text}
    try:
        return {**default, **json_file.load(os.path.join(home, SETTINGS_NAME), {})}
    except (OSError, ValueError):  # a broken file: the defaults rather than stop the loop
        return default


def save_project_settings(home: str, enabled: bool, text: str):
    json_file.save(os.path.join(home, SETTINGS_NAME), {"enabled": enabled, "text": text})


def load_replied() -> dict:
    try:
        return json_file.load(REPLIED_PATH, {})
    except (OSError, ValueError):  # a broken file: start over rather than stop the loop
        return {}


def save_replied(replied: dict):
    json_file.save(REPLIED_PATH, replied)


def rename_worker(old: str, new: str):
    """A worker's file moved to another project (workspaces.move_out): the people it answered stay answered.
    In the running loop's ledger itself: a round saves it whole, a change to the file alone would be lost."""
    replied = _replied if _replied is not None else load_replied()
    moved = [key for key in replied if key.rsplit(":", 1)[0] == old]
    for key in moved:
        replied[f"{new}:{key.rsplit(':', 1)[1]}"] = replied.pop(key)
    if moved:
        save_replied(replied)


def _awaits_reply(dialog) -> bool:
    """A private chat with a live person whose last message is unread and theirs."""
    user = dialog.entity
    if not isinstance(user, types.User):
        return False
    if user.bot or user.is_self or user.deleted or user.id == SERVICE_ID:
        return False
    return dialog.unread_count > 0 and dialog.message is not None and not dialog.message.out


def _describe(user) -> str:
    """Who wrote (HTML): name, @username or the id in <code> (a tap copies it), phone."""
    name = html.escape(full_name(user.first_name, user.last_name))
    handle = f"@{html.escape(user.username)}" if user.username else f"🆔 <code>{user.id}</code>"
    who = f"{name} ({handle})" if name else handle
    return f"{who}, 📱 +{user.phone}" if user.phone else who


def _escaped(text: str, limit: int) -> str:
    """`text` escaped for HTML within `limit` characters: cut before escaping, never inside an
    entity (escaping may grow it fivefold, past a message's 4096)."""
    text = text[:limit]  # escaped, it is never shorter
    escaped = html.escape(text)
    while len(escaped) > limit:
        # a character escapes to 1-6 (&quot;): dropping a sixth of the excess never cuts too much
        text = text[:len(text) - max(1, (len(escaped) - limit) // 6)]
        escaped = html.escape(text)
    return escaped


def profile_url(user) -> str:
    return f"https://t.me/{user.username}" if user.username else f"tg://user?id={user.id}"


async def _unread(client, dialog) -> tuple[list[str], datetime]:
    """The person's unread messages (MAX_FORWARDED at most), oldest first ([медиа] for one without text), and when
    the first of them after the worker's last message was written: their answer's time, not this round's."""
    messages = (await client.get_messages(dialog.entity, limit=min(dialog.unread_count, MAX_FORWARDED)))[::-1]
    last_out = max((i for i, message in enumerate(messages) if message.out), default=-1)
    texts = [message.message or "[медиа]" for message in messages if not message.out]
    return texts, messages[last_out + 1].date  # the chat's last message is theirs (_awaits_reply)


def _incoming(texts: list[str], unread: int) -> str:
    """The unread messages escaped for the HTML notification, how many more there were first."""
    more = [f"…и ещё {unread - MAX_FORWARDED} раньше"] if unread > MAX_FORWARDED else []
    return _escaped("\n".join(more + texts), NOTIFY_TEXT_LIMIT)


def log_dialog(home: str, path: str, user_id: int, text: str, out: bool = False, auto: bool = False,
               at: datetime | None = None):
    """A message of the dialog in the project's outreach log (outreach.log_message): a failure there is logged, the
    reply still handled."""
    try:
        outreach.log_message(home, os.path.basename(path), user_id, text, out, auto, at)
    except OSError as err:
        log.warning("autoreply: outreach log: %s", err)


async def _handle_dialog(client, dialog, path: str, label: str, text: str | None, replied: dict, notify,
                         home: str, project: int) -> int:
    """One unread chat: if it's a mailing reply, its first answer recorded (`home`: the project's folder, for its
    outreach log) and auto-answered (`text`; None: the auto-reply is off), forwarded with ✍️ Ответить (`project`:
    the project's id); 1 if auto-answered."""
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
        if text and (mine[0].message or "").strip() == text.strip():
            # answered in a round whose connection dropped (a job released the worker) before
            # the reply was recorded: record it now instead of answering twice
            replied[key] = datetime.now().strftime(TIME_FORMAT)
            save_replied(replied)
            first = False

    texts, written = await _unread(client, dialog)  # before our reply lands among them
    # logged now, before the auto-reply: the dialog in its order (a round whose notice fails logs them again)
    log_dialog(home, path, dialog.entity.id, "\n".join(texts), at=written)

    answered = 0
    header = "💬 Ответ на рассылку" if first else "💬 Новое сообщение"
    if first and text:
        try:
            await client.send_message(dialog.entity, text, parse_mode=None)  # as typed: no markdown
        except ConnectionError:
            raise
        except Exception as err:  # this person can't be written to (blocked, privacy, a flood limit):
            # the admins still get what they wrote; their next message is another try
            header = ("⚠️ Ответ на рассылку — автоответ НЕ отправлен: "
                      f"{html.escape(str(rich_message.refusal_reason(err) or err))}")
        else:
            replied[key] = datetime.now().strftime(TIME_FORMAT)
            save_replied(replied)  # before anything else can fail: never answer twice
            answered = 1
            header += " — автоответ отправлен"
            log_dialog(home, path, dialog.entity.id, text, out=True, auto=True)
    elif first:  # no auto-reply: its first answer is recorded all the same, the ledger counts who answered
        replied[key] = datetime.now().strftime(TIME_FORMAT)
        save_replied(replied)

    # notify before the read mark: a dropped connection there must not lose it
    reply = ReplyCB(project=project, worker=os.path.basename(path), user=dialog.entity.id)
    delivered = await notify(f"{header}\nОт: {_describe(dialog.entity)}\nВоркер: {html.escape(label)}\n\n"
                             f"{_incoming(texts, dialog.unread_count)}", profile_url(dialog.entity), reply)
    if not delivered:  # no admin got it (the Bot API is down): left unread, the next round forwards it
        return answered

    await client.send_read_acknowledge(dialog.entity, max_id=dialog.message.id)
    return answered


async def poll_worker(client, path: str, label: str, text: str | None, replied: dict, notify, home: str,
                      project: int):
    """Handle the worker's unread mailing replies (_handle_dialog) and forward what answered
    people write later to the admins; returns how many were auto-answered."""
    answered = 0

    async for dialog in client.iter_dialogs(limit=DIALOGS_LIMIT):
        if not _awaits_reply(dialog):
            continue
        try:
            answered += await _handle_dialog(client, dialog, path, label, text, replied, notify, home, project)
        except ConnectionError:
            raise  # the worker itself is gone (e.g. a job disconnected it): its other chats would fail too
        except Exception as err:  # this chat only: the worker's other chats still get their turn
            log.warning("autoreply: %s: chat %s: %s", label, dialog.entity.id, err)

    return answered


async def poll_once(pool, text: str | None, replied: dict, notify, project: int, clients: list):
    """The project's workers `clients`, one at a time (`text`: the auto-reply's, None: off)."""
    for client in clients:
        path = pool.storage.get_session_path(client)
        if path is None:  # replaced (new proxies) or removed since the round began: polled next round
            continue
        if pool.scraping is not None and pool.scraping.path == path:
            continue  # the scraper drives this account from its own client

        username = pool.storage.usernames.get(path)  # @-prefixed, as the 📬 Автоответ screen names it
        label = f"@{username}" if username else os.path.basename(path or "")
        pool.polling = path  # before any await: a scrape must not start on it meanwhile
        async def connect_and_poll():
            await connect_client(client)  # a no-op if a job already has it connected
            await poll_worker(client, path, label, text, replied, notify, pool.storage.home, project)

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


def reply_stats(replied: dict, now: datetime, directory: str) -> dict:
    """How many people the workers in `directory` (a project's sessions/) answered: in all, in the last day /
    week, per worker path."""
    day = week = total = 0
    per_worker = Counter()
    for key, when in replied.items():
        path = key.rsplit(":", 1)[0]
        if os.path.dirname(path) != directory:
            continue
        per_worker[path] += 1
        total += 1
        try:
            age = now - datetime.strptime(when, TIME_FORMAT)
        except (TypeError, ValueError):
            continue
        day += age <= timedelta(days=1)
        week += age <= timedelta(days=7)
    return {"total": total, "day": day, "week": week, "per_worker": per_worker}


async def _send(bot, chat_id, message: str, markup=None):
    """send_message, waiting out the Bot API's flood limit a few times."""
    for attempt in range(NOTIFY_ATTEMPTS):
        try:
            return await bot.send_message(chat_id, message, parse_mode="HTML", reply_markup=markup)
        except TelegramRetryAfter as err:
            if attempt == NOTIFY_ATTEMPTS - 1:
                raise
            await asyncio.sleep(err.retry_after)


def make_notify(bot, admins):
    async def notify(message: str, url: str, reply: ReplyCB | None = None) -> bool:
        """`message`: HTML (whatever came from Telegram escaped); `reply`: its ✍️ Ответить. True if at least one
        admin got it."""
        delivered = False
        answer = []
        if reply:
            try:
                answer = [InlineKeyboardButton(text="✍️ Ответить", callback_data=reply.pack())]
            except ValueError:  # a hand-placed .session's long name: past a button's 64 bytes, the notice goes without
                log.warning("autoreply: no ✍️ for %s: its file name is too long", reply.worker)
        markup = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="👤 Открыть профиль", url=url)], *([answer] if answer else [])]
        )
        for admin in admins:
            try:
                try:
                    await _send(bot, admin, message, markup)
                except TelegramBadRequest:  # a tg://user link the person's privacy forbids
                    await _send(bot, admin, message,
                                InlineKeyboardMarkup(inline_keyboard=[answer]) if answer else None)
                delivered = True
            except Exception as err:
                log.warning("autoreply: notify %s: %s", admin, err)
        return delivered
    return notify


def due_workers(pool, due: dict, now: float, interval: list[int]) -> list:
    """The pool's workers whose check is due at `now` (time.monotonic), their next one drawn then
    (pick_seconds(interval)); `due`: session path -> its next check. A worker not there yet gets its first at a
    random point of the longest interval: the workers of a project (or of the bot's start) spread at once."""
    ready = []
    for client in pool.workers:
        path = pool.storage.get_session_path(client)
        if path is None:
            continue
        if path not in due:
            due[path] = now + random.uniform(0, max(interval))
        if due[path] <= now:
            due[path] = now + pick_seconds(interval)
            ready.append(client)
    return ready


async def _loop(bot, workspaces, settings, admins):
    global _replied
    replied = _replied = load_replied()
    notify = make_notify(bot, admins)
    due = {}  # session path -> its next check (time.monotonic): each worker's own timer

    try:
        while True:
            now, polled = time.monotonic(), set()
            for workspace in workspaces.all():  # every project's workers: projects deleted meanwhile are gone
                pool = workspace.pool
                home = pool.storage.home
                own = project_settings(home, settings)
                text = own["text"] if own["enabled"] and own["text"] else None
                # once the project has mailed anyone (its ledger there), its replies are tracked
                if text is None and not os.path.exists(os.path.join(home, MAILING_STATS)):
                    continue  # nothing to track nor answer here yet: looked at again next time
                ready = due_workers(pool, due, now, settings.autoreply_interval)
                polled.update(pool.storage.get_session_path(client) for client in pool.workers)
                if not ready:
                    continue
                try:
                    await poll_once(pool, text, replied, notify, workspace.project_id, ready)
                except Exception as err:  # never let one bad round end the loop
                    log.warning("autoreply: round failed: %s", err)
            for path in set(due) - polled:  # gone, or its project not polled now: a new first check when back
                del due[path]
            wait = min(due.values()) - time.monotonic() if due else TICK  # until the next worker's check
            await asyncio.sleep(min(TICK, max(1, wait)))
    finally:
        _replied = None


async def start(bot, workspaces, settings, admins):
    """Bot startup: run the loop in the background; it skips the projects that never mailed, their auto-reply off."""
    global _task
    # kept referenced, or the loop's weak ref lets GC drop it
    _task = asyncio.create_task(_loop(bot, workspaces, settings, admins))


async def stop():
    if _task is not None:
        _task.cancel()
