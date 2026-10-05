from bot.services.capture import capture
from bot.services.registry import BOT_FUNCTIONS_BY_KEY

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


def resolve(functions: dict, key: str):
    """Return (instance, BotFunction) for a registry key.

    Every classname is checked against the discovered functions at bot startup
    (see bot/app.py), so the lookup here cannot miss.
    """
    bot_function = BOT_FUNCTIONS_BY_KEY[key]
    return functions[bot_function.classname], bot_function


async def ensure_workers(callback, pool) -> bool:
    """True if there is at least one worker; otherwise alert the operator."""
    if pool.count() == 0:
        await callback.message.answer("Нет воркер-аккаунтов. Добавьте сессии в sessions/.")
        return False
    return True


async def read_text_document(message, max_size: int) -> str | None:
    """Text of a .txt the user sent; report and return None if it's too big."""
    if (message.document.file_size or 0) > max_size:
        await message.answer("Файл слишком большой. Пришлите .txt поменьше.")
        return None
    buffer = await message.bot.download(message.document)
    return buffer.read().decode("utf-8", "replace")


async def require_text(message) -> str | None:
    """Text of a text-only FSM step, stripped; re-prompt and return None if the user
    sent a non-text message (sticker/photo/…), so the step isn't lost to AttributeError."""
    text = (message.text or "").strip()
    if not text:
        await message.answer("Ожидается текст. Попробуйте ещё раз.")
        return None
    return text
