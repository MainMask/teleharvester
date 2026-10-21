from bot.keyboards.common import PAGE_SIZE, cancel_kb
from bot.keyboards.menu import back_kb, main_menu
from bot.services import login as logins
from bot.services.capture import capture
from bot.services.jobs import busy
from modules.registry import BOT_FUNCTIONS_BY_KEY, menu_path
from functions.base.base import POST_LINK_HINT, BaseFunction
from scraper.scrape import bad_channels, bad_channels_text, parse_channels

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


NO_WORKERS = f"У проекта нет воркер-аккаунтов. Добавьте: {menu_path('add')}."


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


async def back_answer(message, text: str, screen: str, project: int = 0, **kwargs):
    """A flow's last word with ⬅️ to the screen it was started from (in its `project`): never a dead end."""
    await message.answer(text, reply_markup=back_kb(screen, project), **kwargs)


async def busy_step(message, manager, screen: str):
    """A form's last step while the project's job slot is busy: said, with ⬅️ to `screen`; the step stays, so
    what was sent is not lost — it can be sent again once that job is over."""
    await back_answer(message, f"{busy(manager.label)}\nКогда она закончится, пришлите сообщение ещё раз.", screen,
                      manager.project_id)


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
        await message.answer("Нужен текст. Пришлите ещё раз:")
        return None
    return text
