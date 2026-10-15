from bot.keyboards.common import PAGE_SIZE
from bot.keyboards.menu import main_menu
from bot.services import login as logins
from bot.services.capture import capture
from modules.registry import BOT_FUNCTIONS_BY_KEY, menu_path
from functions.base.base import POST_LINK_HINT, BaseFunction
from scraper.scrape import bad_channels, bad_channels_text, parse_channels

# a stale tap: a button of an old message; a stale step: text sent to a step whose flow is gone
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
    (unsupported type like a poll/contact/location), so the flow aborts instead of
    failing silently on every recipient."""
    try:
        content = await capture(message, message.bot, album)
    except Exception as err:
        await message.answer(f"Не удалось обработать сообщение для рассылки: {err}")
        return None

    if not content.text and not content.media:
        content.cleanup()
        await message.answer("Это сообщение нельзя разослать (пустое или неподдерживаемый тип).")
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


def resolve(functions: dict, key: str):
    """Return (a new instance, BotFunction) for a registry key: each run has its own.

    Every classname is checked against the discovered functions at bot startup
    (see bot/app.py), so the lookup here cannot miss.
    """
    bot_function = BOT_FUNCTIONS_BY_KEY[key]
    return functions[bot_function.classname](), bot_function


async def ensure_workers(callback, pool) -> bool:
    """True if there is at least one worker; otherwise alert the operator."""
    if pool.count() == 0:
        await callback.message.answer(f"У проекта нет воркер-аккаунтов. Добавьте: {menu_path('add')}.")
        return False
    return True


async def send_chunked(message, header: str, lines: list[str], sep: str = "\n\n"):
    """Send header + cards (blank line between, or `sep`) in message-sized chunks."""
    chunks, current = [], header
    for line in lines:
        if len(current) + len(line) + len(sep) > MAX_MESSAGE:
            chunks.append(current)
            current = ""
        current += (sep if current else "") + line
    chunks.append(current)

    for chunk in chunks:
        await message.answer(chunk, parse_mode="HTML")


async def menu_answer(message, text: str):
    """Send a closing message and return the operator to the main menu."""
    await message.answer(text, reply_markup=main_menu())


async def leave_form(message, state):
    """A main-menu button or /start: out of any form; a sign-in by phone waiting for its code is
    closed now, as /cancel does, not after its timeout."""
    await logins.close(message.chat.id)
    await state.clear()


async def read_text_document(message, max_size: int) -> str | None:
    """Text of a .txt the user sent; report and return None if it's too big."""
    if (message.document.file_size or 0) > max_size:
        await message.answer("Файл слишком большой. Пришлите .txt поменьше.")
        return None
    buffer = await message.bot.download(message.document)
    return buffer.read().decode("utf-8", "replace")


async def left_flow(callback, state, step) -> bool:
    """A stale button: the chat is no longer at `step` (the State its keyboard was shown at) but in
    another flow, or none. Says so and answers the tap; the caller then changes nothing."""
    if await state.get_state() == step.state:
        return False
    await callback.answer()
    await callback.message.answer(STALE_BUTTON)
    return True


async def require_post_link(message) -> str | None:
    """A post's link of a text step (t.me/name/123, t.me/c/…/123); re-prompt and return None if it is
    none, so the step stays instead of every worker failing on it."""
    link = await require_text(message)
    if link is not None and not BaseFunction.is_message_link(link):
        await message.answer(f"{POST_LINK_HINT} Пришлите ещё раз:")
        return None
    return link


async def typed_channels(message, raw: str) -> list[str] | None:
    """The typed channels; None after asking for them again (none, or one that can't be a channel)."""
    channels = parse_channels(raw)
    if not channels:
        await message.answer("Не вижу ни одного канала. Пришлите ещё раз:")
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
        await message.answer("Ожидается текст. Попробуйте ещё раз.")
        return None
    return text
