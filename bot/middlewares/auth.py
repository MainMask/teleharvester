from aiogram import BaseMiddleware


class AuthMiddleware(BaseMiddleware):
    """Whitelist: only admin user IDs pass; everyone else is dropped silently."""

    def __init__(self, admins: set[int]):
        self.admins = admins

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")

        if user is None or user.id not in self.admins:
            return None

        return await handler(event, data)
