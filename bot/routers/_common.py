from bot.services.registry import BOT_FUNCTIONS_BY_KEY


def resolve(functions: dict, key: str):
    """Return (instance, BotFunction) for a registry key."""
    bot_function = BOT_FUNCTIONS_BY_KEY[key]
    return functions[bot_function.classname], bot_function


async def ensure_workers(callback, pool) -> bool:
    """True if there is at least one worker; otherwise alert the operator."""
    if pool.count() == 0:
        await callback.message.answer("Нет воркер-аккаунтов. Добавьте сессии в sessions/.")
        return False
    return True
