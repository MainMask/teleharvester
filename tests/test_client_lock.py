"""connect_client / release_client never overlap on one client (modules/storages/sessions_storage.py).

Telethon's disconnect() doesn't take the lock its connect() does: a connect() while the same client
is disconnecting left it "connected" with no connection, every request hanging until a restart.
"""

import asyncio
import collections
import logging

from telethon.crypto import AuthKey
from telethon.network.mtprotosender import MTProtoSender

from modules.storages.sessions_storage import connect_client, release_client


class _Loggers(collections.defaultdict):
    def __missing__(self, key):
        return logging.getLogger(key)


class _Transport:
    """A Telethon connection that takes a moment to open and to close, and never answers."""

    _connected = False

    async def connect(self, timeout=None):
        await asyncio.sleep(0.05)
        self._connected = True

    async def disconnect(self):
        self._connected = False
        await asyncio.sleep(0.01)

    async def send(self, data):
        await asyncio.sleep(3600)

    async def recv(self):
        await asyncio.sleep(3600)


class _Client:
    """A TelegramClient's connect/disconnect over a real MTProtoSender."""

    def __init__(self):
        self.sender = MTProtoSender(AuthKey(b"\0" * 256), loggers=_Loggers())
        self.session = None  # release_client's cache clear is skipped

    async def connect(self):
        await self.sender.connect(_Transport())

    async def disconnect(self):
        await self.sender.disconnect()


def test_connect_during_a_release_waits_for_it():
    async def scenario():
        client = _Client()
        await connect_client(client)
        # a job releases the worker while the autoreply (or the next job) connects it
        release = asyncio.create_task(release_client(client))
        await asyncio.sleep(0)  # the release is now closing the transport
        await connect_client(client)
        await release
        return client.sender

    sender = asyncio.run(scenario())

    assert sender.is_connected() and sender._connection is not None  # really connected, not a shell
    asyncio.run(sender.disconnect())


def test_release_during_a_connect_waits_for_it():
    async def scenario():
        client = _Client()
        connect = asyncio.create_task(connect_client(client))
        await asyncio.sleep(0)  # the connect is now opening the transport
        await release_client(client)
        await connect
        return client.sender

    sender = asyncio.run(scenario())

    assert not sender.is_connected() and sender._connection is None
