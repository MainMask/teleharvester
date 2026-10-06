"""Offline tests for gathering an album's messages into one handler call (bot/services/album.py)."""

import asyncio
import types

from bot.services.album import AlbumMiddleware


def _part(message_id, group="g1"):
    return types.SimpleNamespace(message_id=message_id, media_group_id=group)


def test_a_slowly_arriving_album_is_one_call():
    """Parts 0.05 s apart (under the latency) but spread over more than it: still one album."""
    middleware = AlbumMiddleware(latency=0.1)
    calls = []

    async def handler(event, data):
        calls.append([m.message_id for m in data["album"]])

    async def scenario():
        tasks = []
        for message_id in (1, 2, 3, 4):
            tasks.append(asyncio.create_task(middleware(handler, _part(message_id), {})))
            await asyncio.sleep(0.05)
        await asyncio.gather(*tasks)

    asyncio.run(scenario())
    assert calls == [[1, 2, 3, 4]]


def test_a_single_message_passes_through():
    middleware = AlbumMiddleware(latency=0.1)
    seen = []

    async def handler(event, data):
        seen.append(data["album"])

    asyncio.run(middleware(handler, _part(1, group=None), {}))
    assert seen == [None]
