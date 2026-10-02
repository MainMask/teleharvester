import asyncio

from aiogram import BaseMiddleware
from aiogram.types import Message


class AlbumMiddleware(BaseMiddleware):
    """Aggregate album (media_group) messages into one handler call.

    Telegram sends each album item as a separate update. The first item of a group
    waits `latency` for the rest to arrive, then runs the handler once with the whole
    list injected as `album`; later items are swallowed. Single messages pass through
    with `album=None`.
    """

    def __init__(self, latency: float = 0.7):
        self.latency = latency
        self.albums: dict[str, list[Message]] = {}

    async def __call__(self, handler, event: Message, data):
        group_id = getattr(event, "media_group_id", None)
        if group_id is None:
            data["album"] = None
            return await handler(event, data)

        bucket = self.albums.setdefault(group_id, [])
        bucket.append(event)

        if len(bucket) > 1:  # not the leader; it will carry the whole group
            return

        await asyncio.sleep(self.latency)
        album = self.albums.pop(group_id, [event])
        album.sort(key=lambda message: message.message_id)
        data["album"] = album
        return await handler(event, data)
