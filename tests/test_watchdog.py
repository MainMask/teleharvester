"""bot/services/watchdog.py: systemd watchdog pings from the event loop."""

import asyncio
import os
import socket
import tempfile

from bot.services import watchdog


def test_returns_at_once_outside_systemd(monkeypatch):
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)

    asyncio.run(asyncio.wait_for(watchdog.run(), 1))  # no pings to send: done, not looping


def test_pings_the_notify_socket(monkeypatch):
    # a short dir: a unix socket path is limited to ~104 bytes (macOS's TMPDIR is long)
    directory = tempfile.mkdtemp(dir="/tmp")
    path = os.path.join(directory, "notify")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(path)
    server.settimeout(2)
    monkeypatch.setenv("NOTIFY_SOCKET", path)
    monkeypatch.setenv("WATCHDOG_USEC", "200000")  # a ping every 0.1 s

    async def scenario():
        task = asyncio.create_task(watchdog.run())
        await asyncio.sleep(0.25)
        task.cancel()

    try:
        asyncio.run(scenario())
        assert server.recv(64) == b"WATCHDOG=1"
        assert server.recv(64) == b"WATCHDOG=1"  # it keeps pinging
    finally:
        server.close()
        os.remove(path)
        os.rmdir(directory)


def test_abstract_socket_address():
    assert watchdog.notify_address("@systemd/notify") == "\0systemd/notify"
    assert watchdog.notify_address("/run/systemd/notify") == "/run/systemd/notify"
