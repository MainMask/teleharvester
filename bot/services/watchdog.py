"""systemd watchdog (WatchdogSec= in deploy/teleharvester-bot.service).

The bot pings systemd from its event loop: a crash is restarted by Restart=on-failure, but a
hung loop (a blocking call with no timeout, a deadlock) would leave a live process that
answers nothing. With the pings stopped, systemd kills it and restarts it like a crash.
"""
import asyncio
import os
import socket


def notify_address(address: str) -> str:
    """$NOTIFY_SOCKET as a socket address: "@name" is an abstract socket ("\\0name")."""
    return "\0" + address[1:] if address.startswith("@") else address


async def run():
    """Ping the watchdog twice per WatchdogSec (as sd_watchdog_enabled advises) for as long as
    the loop runs; returns at once when not under a systemd watchdog (plain `python -m bot`)."""
    usec, address = os.environ.get("WATCHDOG_USEC"), os.environ.get("NOTIFY_SOCKET")
    if not usec or not address:
        return

    interval = int(usec) / 2_000_000
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        while True:
            try:
                sock.sendto(b"WATCHDOG=1", notify_address(address))
            except OSError:  # a transient send error must not end the pings: the next one may pass
                pass
            await asyncio.sleep(interval)
