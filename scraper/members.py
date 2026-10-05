"""Export group member lists into a participants base (for the mailing / adding to contacts).

Unlike a scrape, which finds people through messages, comments and reactions in a date
window, this takes everyone in the member list, the silent ones too. Telegram serves a
plain listing up to ~10 000 members; past that the rest is reached by searching names
letter by letter. A broadcast channel's subscribers, or a group with a hidden member
list, are visible to admins only.
"""
import asyncio
import string
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from telethon.errors import ChatAdminRequiredError, FloodWaitError
from telethon.tl.types import InputPeerUser

from scraper.config import Credentials
from scraper.datafiles import save_table
from scraper.scrape import _channel_ref, _entity_name, _user_access_hash, _warm_channel, connect, watch_stop

# name searches that reach the members a plain listing leaves out
SEARCH_CHARS = string.ascii_lowercase + "абвгдеёжзийклмнопрстуфхцчшщэюя" + string.digits


@dataclass
class MembersParams:
    chats: list[str]
    name: str
    out_dir: Path = Path("output")
    # called with (members listed so far, the chat's member count) for the current chat
    on_progress: Callable[[int, int], None] | None = None
    # set by the run: the account that listed; its access hashes are the only valid ones
    owner_id: int | None = None
    # set from another thread to stop: what is listed so far is saved, the rest skipped
    stop: threading.Event | None = None


async def _collect(client, entity, group: str, members: dict, params: MembersParams) -> tuple[int, int]:
    """Add the chat's members to `members` ({id: row}); return (listed, member count)."""
    seen: set[int] = set()  # this chat's ids, bots included: what the member count counts
    total = 0

    def add(user):
        seen.add(user.id)
        if params.on_progress is not None:
            params.on_progress(min(len(seen), total), total)
        if user.bot or user.deleted or user.id == params.owner_id:
            return
        access_hash = _user_access_hash(user)
        if access_hash is None:
            return
        members.setdefault(user.id, {"ID": user.id, "Username": user.username or "",
                                     "Access Hash": access_hash, "Name": _entity_name(user),
                                     "Group": group})

    listing = client.iter_participants(entity)
    async for user in listing:
        total = listing.total or 0  # known once the first page is in
        add(user)
    total = max(total, len(seen))

    if len(seen) < total:
        print(f"  listed {len(seen)} of {total}; searching names for the rest")
        for char in SEARCH_CHARS:
            async for user in client.iter_participants(entity, search=char):
                if user.id not in seen:
                    add(user)
            if len(seen) >= total:
                break
    return len(seen), total


async def _run(creds: Credentials, params: MembersParams) -> tuple[dict, list[tuple[str, str]]]:
    members: dict = {}
    problems: list[tuple[str, str]] = []  # (chat, why it is missing or incomplete)
    dialogs_loaded = False
    client = None
    # before connecting: ⏹ must work while a dead proxy is retried for hours
    watcher = watch_stop(params.stop)
    try:
        client = await connect(creds)
        params.owner_id = (await client.get_me()).id
        for i, chat in enumerate(params.chats):
            print(f"=== chat {i + 1}/{len(params.chats)}: {chat} ===")
            try:
                ref = _channel_ref(chat)
                dialogs_loaded = await _warm_channel(client, ref, dialogs_loaded)
                entity = await client.get_input_entity(ref.arg)
                if isinstance(entity, InputPeerUser):
                    problems.append((chat, "a private chat has no member list"))
                    continue
                listed, total = await _collect(client, entity, f"@{ref.slug}", members, params)
                print(f"  {listed} of {total} members")
                if listed < total:
                    problems.append((chat, f"Telegram gave {listed} of {total} members"))
            except asyncio.CancelledError:
                if params.stop is None or not params.stop.is_set():
                    raise  # a real cancellation (Ctrl-C), not the operator's stop
                asyncio.current_task().uncancel()
                problems.append((chat, "stopped - the members listed so far are saved"))
                break
            except ChatAdminRequiredError:
                problems.append((chat, "the member list is hidden or admins-only "
                                       "(a channel's subscribers)"))
            except FloodWaitError as exc:  # a long wait: keep what is listed, skip the rest
                problems.append((chat, f"FLOOD_WAIT {exc.seconds}s - stopped, the rest skipped"))
                break
            except Exception as exc:  # unknown chat, not a member, ...
                problems.append((chat, f"{type(exc).__name__}: {exc}"))
    except asyncio.CancelledError:  # a chat's own stop is handled above: this one came while connecting
        if params.stop is None or not params.stop.is_set():
            raise
        asyncio.current_task().uncancel()
        problems.append((", ".join(params.chats), "stopped before the listing started"))
    finally:
        if watcher is not None:
            watcher.cancel()
        if client is not None:
            await client.disconnect()
    return members, problems


def run(creds: Credentials, params: MembersParams) -> tuple[Path | None, list[tuple[str, str]]]:
    """List the chats' members into <out_dir>/<name>_participants_members.parquet.

    Returns (the file, or None if nobody was listed; [(chat, problem)])."""
    members, problems = asyncio.run(_run(creds, params))
    for chat, problem in problems:
        print(f"  ! {chat}: {problem}")
    if not members:
        return None, problems

    rows = list(members.values())
    df = pd.DataFrame(rows)
    # int64 + None would become float64 and corrupt the hash; keep a stable type
    df["Access Hash"] = pd.array([r["Access Hash"] for r in rows], dtype="Int64")
    df["ID"] = df["ID"].astype("Int64")
    if params.owner_id is not None:
        df["Owner ID"] = pd.array([params.owner_id] * len(df), dtype="Int64")
    path = save_table(df, params.out_dir / f"{params.name}_participants_members", "parquet")
    print(f"Members: {path}  ({len(df)} people)")
    return path, problems
