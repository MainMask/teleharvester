import asyncio
import datetime

from aiohttp import ClientError
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InaccessibleMessage

from bot.keyboards.common import PAGE_SIZE, cancel_kb
from bot.keyboards.menu import back_kb, main_menu
from bot.services import login as logins
from bot.services.capture import capture
from bot.services.jobs import busy
from modules.registry import BOT_FUNCTIONS_BY_KEY, NO_WORKERS
from functions.base.base import POST_LINK_HINT, BaseFunction
from scraper.scrape import CHAT_FORMAT, bad_channels, bad_channels_text, parse_channels  # noqa: F401 — CHAT_FORMAT: the
# routers' prompts take it from here

# a stale tap: a button of an old message (stale_tap); a stale step: text sent to a step whose flow is gone
STALE_BUTTON = "Кнопка устарела — откройте раздел заново."
STALE_STEP = "Действие устарело — откройте раздел заново."
MAX_MESSAGE = 4000  # send_chunked keeps each chunk under Telegram's 4096-char limit

SEND_MESSAGE_PROMPT = (
    "Отправьте сообщение для рассылки — текст, медиа или альбом, "
    "с любым форматированием и кастомными эмодзи. Оно будет отправлено как есть."
)


async def build_content(message, album):
    """Capture the broadcast message; report and return None if media can't be fetched
    (e.g. the Bot API's ~20 MB download cap) or the message has nothing to send
    (unsupported type like a poll/contact/location), so the step asks for another instead of
    failing silently on every recipient (the caller leaves its step only once it has the content)."""
    try:
        content = await capture(message, message.bot, album)
    except Exception as err:
        await message.answer(f"Не удалось обработать сообщение для рассылки: {err}. Пришлите другое:")
        return None

    if not content.text and not content.media:
        content.cleanup()
        await message.answer("Это сообщение нельзя разослать (пустое или неподдерживаемый тип). Пришлите другое:")
        return None

    return content


def marks_step(value: str, picked: list[int], page: int, total: int, presets: dict) -> tuple[list[int], int]:
    """A tap on a marks_kb list: (the ticked indexes, the page) after it — an index ticks or unticks, ⬅️ / ➡️
    turn the page round, a preset's value (☑️ Все…) ticks its own list."""
    pages = max(1, -(-total // PAGE_SIZE))
    if value in presets:
        return list(presets[value]), page
    if value == "prev":
        return picked, (page - 1) % pages
    if value == "next":
        return picked, (page + 1) % pages
    if value.isdigit() and int(value) < total:
        return sorted(set(picked) ^ {int(value)}), page
    return picked, page  # 📄: the page shown


def pick_from(items: list, value: str):
    """The item a button names by its index into `items` (its value); None for a stale one."""
    index = int(value) if value.isdigit() else -1
    return items[index] if 0 <= index < len(items) else None


def resolve(functions: dict, key: str):
    """Return (a new instance, BotFunction) for a registry key: each run has its own.

    Every classname is checked against the discovered functions at bot startup
    (see bot/app.py), so the lookup here cannot miss.
    """
    bot_function = BOT_FUNCTIONS_BY_KEY[key]
    return functions[bot_function.classname](), bot_function


async def ensure_workers(callback, pool) -> bool:
    """A function's button tapped (answered here): True if the project has a worker; else False after saying
    so in a pop-up over its screen."""
    if pool.count() == 0:
        await callback.answer(NO_WORKERS, show_alert=True)
        return False
    await callback.answer()
    return True


async def start_form(callback, state, step, text: str, pool=None, markup=None, parse_mode=None,
                     data: dict | None = None) -> bool:
    """A function's first step from its screen's button: with `pool`, none without a worker (ensure_workers);
    else the chat at `step` (a form before it left), `data` kept for the next steps, and the step's prompt
    (`text`, which starts with the function's title) with ✖️ Отмена under its own buttons (`markup`): a
    choice of them in the screen's place, a prompt for text as a new message. False: not started."""
    if pool is not None:
        if not await ensure_workers(callback, pool):
            return False
    else:
        await callback.answer()
    await state.clear()
    await state.set_state(step)
    if data:
        await state.update_data(**data)
    send = callback.message.edit_text if markup is not None else callback.message.answer
    await send(text, parse_mode=parse_mode, reply_markup=cancel_kb(step, markup))
    return True


async def send_chunked(message, header: str, lines: list[str], sep: str = "\n\n", reply_markup=None):
    """Send header + cards (blank line between, or `sep`) in message-sized chunks; `reply_markup` under the last."""
    chunks, current = [], header
    for line in lines:
        if len(current) + len(line) + len(sep) > MAX_MESSAGE:
            chunks.append(current)
            current = ""
        current += (sep if current else "") + line
    chunks.append(current)

    for index, chunk in enumerate(chunks, 1):
        await message.answer(chunk, parse_mode="HTML", reply_markup=reply_markup if index == len(chunks) else None)


def fit_lines(lines: list[str], limit: int = MAX_MESSAGE, more: int = 0) -> str:
    """`lines` (HTML) joined, whole ones within `limit` characters — a cut one could split a tag or an &entity; —,
    then "…и ещё N" for the ones left out (`more`: left out before)."""
    kept, size = [], len("\n…и ещё 99999")
    for line in lines:
        size += len(line) + 1
        if size > limit:
            break
        kept.append(line)
    if left := len(lines) - len(kept) + more:
        kept.append(f"…и ещё {left}")
    return "\n".join(kept)


async def menu_answer(message, text: str):
    """Send a closing message and return the operator to the main menu."""
    await message.answer(text, reply_markup=main_menu())


async def back_answer(message, text: str, screen: str, project: int = 0, **kwargs):
    """A flow's last word with ⬅️ to the screen it was started from (in its `project`): never a dead end."""
    await message.answer(text, reply_markup=back_kb(screen, project), **kwargs)


async def busy_step(message, manager, screen: str, again: str = "пришлите сообщение ещё раз"):
    """A form's last step while the project's job slot is busy: said, with ⬅️ to `screen`; the step stays, so
    what was sent is not lost — it can be sent again (`again`) once that job is over."""
    await back_answer(message, f"{busy(manager.label)}\nКогда она закончится, {again}.", screen, manager.project_id)


async def busy_tap(callback, manager) -> bool:
    """A form's last button while the project's job slot is busy: True after saying so in a pop-up over it; the
    step and its buttons stay, to be tapped again once that job is over."""
    if not manager.active:
        return False
    await callback.answer(f"{busy(manager.label)}\nКогда она закончится, нажмите ещё раз.", show_alert=True)
    return True


async def leave_form(message, state):
    """A main-menu button or /start: out of any form; a sign-in by phone waiting for its code is
    closed now, as /cancel does, not after its timeout."""
    await logins.close(message.chat.id)
    await state.clear()


# the confirmation of an irreversible step: an old message's «Да» must not run it days later
CONFIRM_TTL = datetime.timedelta(minutes=10)


def confirm_expired(message, ttl: datetime.timedelta = CONFIRM_TTL) -> bool:
    """A confirmation older than `ttl` (inaccessible: too old for the bot to read, its date 0): by its last edit,
    so one asked in a screen's place (an edit of it) is as old as the question, not as the screen
    (Telegram gives edit_date as a unix time, date as a datetime)."""
    if isinstance(message, InaccessibleMessage):
        return True
    asked = datetime.datetime.fromtimestamp(message.edit_date, datetime.timezone.utc) if message.edit_date \
        else message.date
    return datetime.datetime.now(datetime.timezone.utc) - asked > ttl


DOWNLOAD_TIMEOUT = 300  # seconds: the Bot API fetches a big file from Telegram first, often past its usual 60


async def download(message, document) -> bytes | None:
    """A file the user sent; None after asking for it again if Telegram didn't give it (a timeout, a file too
    big for the Bot API): the step stays."""
    try:
        file = await message.bot.get_file(document.file_id, request_timeout=DOWNLOAD_TIMEOUT)
        return (await message.bot.download_file(file.file_path, timeout=DOWNLOAD_TIMEOUT)).read()
    except (TelegramAPIError, ClientError, asyncio.TimeoutError, OSError) as err:
        await message.answer(f"⚠️ Не удалось получить файл {document.file_name or ''} от Telegram ({err}). "
                             "Пришлите его ещё раз:")
        return None


async def read_text_document(message, max_size: int) -> str | None:
    """Text of a .txt the user sent; report and return None if it's too big or Telegram didn't give it."""
    if (message.document.file_size or 0) > max_size:
        await message.answer("Файл слишком большой. Пришлите .txt поменьше:")
        return None
    data = await download(message, message.document)
    return None if data is None else data.decode("utf-8", "replace")


async def left_flow(callback, state, step) -> bool:
    """A stale button: the chat is no longer at `step` (the State its keyboard was shown at) but in
    another flow, or none. Says so over the message (stale_tap); the caller then changes nothing."""
    if await state.get_state() == step.state:
        return False
    await stale_tap(callback)
    return True


async def stale_tap(callback):
    """A button of a message left behind: said in a pop-up over it, as by the last router (stale.py), no
    message piled up under it."""
    await callback.answer(STALE_BUTTON, show_alert=True)


async def require_post_link(message) -> str | None:
    """A post's link of a text step (t.me/name/123, t.me/c/…/123); re-prompt and return None if it is
    none, so the step stays instead of every worker failing on it."""
    link = await require_text(message)
    if link is not None and not BaseFunction.is_message_link(link):
        await message.answer(f"{POST_LINK_HINT} Пришлите ещё раз:")
        return None
    return link


async def typed_channels(message, raw: str) -> list[str] | None:
    """The typed channels or groups; None after asking for them again (none, or one that can't be a chat)."""
    channels = parse_channels(raw)
    if not channels:
        await message.answer("Не вижу ни одного канала или группы. Пришлите ещё раз:")
        return None
    if bad := bad_channels(channels):
        await message.answer(f"{bad_channels_text(bad)} Пришлите список ещё раз:")
        return None
    return channels


async def delete_quietly(message):
    """Best effort: a password or a login code must not stay in the chat history."""
    try:
        await message.delete()
    except Exception:
        pass


async def require_text(message) -> str | None:
    """Text of a text-only FSM step, stripped; re-prompt and return None if the user
    sent a non-text message (sticker/photo/…), so the step isn't lost to AttributeError."""
    text = (message.text or "").strip()
    if not text:
        await message.answer("Нужен текст. Пришлите ещё раз:")
        return None
    return text
