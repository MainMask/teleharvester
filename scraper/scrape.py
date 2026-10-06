"""Async Telegram scraper (terminal port of the original notebook cells 1-3)."""

import asyncio
import json
import re
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from telethon import TelegramClient, utils
from telethon.sessions import SQLiteSession, StringSession
from telethon.errors import ChannelPrivateError, FloodWaitError, ServerError, TimedOutError
from telethon.tl.functions.messages import GetForumTopicsByIDRequest, GetMessageReactionsListRequest
from telethon.tl.types import (
    ForumTopic, InputPeerChannel, InputPeerChat, InputPeerUser, MessageService, PeerChannel, PeerChat,
    PeerUser, User,
)

from scraper.config import Credentials
from scraper.datafiles import clean_xml_text, format_duration

SEP = "-" * 80

# Telethon auto-sleeps and retries the exact request on FLOOD_WAIT up to this many
# seconds (its default is only 60), so ordinary rate limits and short soft bans are
# waited out transparently instead of skipping the data. Longer waits raise
# FloodWaitError, which the channel loop checkpoints and waits out itself.
FLOOD_SLEEP_THRESHOLD = 3600
# a FloodWaitError longer than this (or too many in a row without progress) stops the run
# (continued later from its checkpoint) rather than sleeping for the better part of a day.
FLOOD_MAX_WAIT = 6 * 3600
FLOOD_MAX_ATTEMPTS = 12
FLOOD_RETRY_BUFFER = 5  # extra seconds slept on top of the ban so we don't re-trip it
# small pause after each reaction-list request to keep the burst rate down
REACTOR_CALL_DELAY = 0.5

# A channel post's comment counter (message.replies.replies) can briefly lag behind
# reality on a very fresh post. Older than this, a 0 count is trusted as "no
# comments" and the GetReplies call is skipped (saves one request per empty post
# and keeps the log clean); within it, the thread is always fetched.
RECENT_POST_WINDOW = timedelta(hours=48)

# Keep reconnecting through long outages instead of aborting the run. Bounded
# wall time ~ CONNECTION_RETRIES * (RETRY_DELAY + connect time) ~ 8-14h, enough
# for a multi-hour outage, still finite (never None/negative, i.e. infinite).
CONNECTION_RETRIES = 2000  # Telethon default 5
RETRY_DELAY = 15           # seconds between reconnect attempts; default 1
REQUEST_RETRIES = 10       # per-request retries across reconnects; default 5

# every TelegramClient of a long run (scrape, verify, members) is built with these
CLIENT_KWARGS = dict(flood_sleep_threshold=FLOOD_SLEEP_THRESHOLD,
                     connection_retries=CONNECTION_RETRIES,
                     retry_delay=RETRY_DELAY,
                     request_retries=REQUEST_RETRIES,
                     # re-raise the last 500/503 once retries run out, not Telethon's
                     # generic ValueError, so RETRYABLE_RPC below actually catches it
                     raise_last_call_error=True)


class _EntityCacheSession(SQLiteSession):
    """Telethon's cache of every user/chat seen, kept in a SQLite file instead of RAM: a
    StringSession holds it all in memory, unbounded over millions of scraped reactors.
    The auth key stays in memory only - the file is a cache, not a login."""

    def _update_session_table(self):
        key, self._auth_key = self._auth_key, None  # store the row without the key
        try:
            super()._update_session_table()
        finally:
            self._auth_key = key


def watch_stop(stop: threading.Event | None) -> asyncio.Task | None:
    """Cancel the current task once `stop` is set from another thread (the bot's ⏹) - even
    mid-sleep, e.g. a long flood wait, unlike a flag checked between requests. The caller
    cancels the returned watcher when its run ends."""
    if stop is None:
        return None
    task = asyncio.current_task()

    async def watch():
        while not stop.is_set():
            await asyncio.sleep(1)
        task.cancel()

    return asyncio.create_task(watch())


async def connect(creds: Credentials, entity_cache: Path | None = None) -> TelegramClient:
    """The run's own client on the account's auth key, with its proxy and device.

    entity_cache: a file for Telethon's entity cache (flat memory on a giant run); None
    keeps it in memory. connect(), not start(): on a session that is no longer authorized
    start() would wait for a phone number on stdin, hanging the bot's scrape thread."""
    session = StringSession(creds.session_string)
    if entity_cache is not None:
        cache = _EntityCacheSession(str(entity_cache))
        cache.set_dc(session.dc_id, session.server_address, session.port)
        cache.auth_key = session.auth_key
        session = cache
    client = TelegramClient(session, creds.api_id, creds.api_hash,
                            proxy=creds.proxy, **(creds.device or {}), **CLIENT_KWARGS)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise SystemExit("the worker's session is no longer authorized - re-add the account")
    return client

# In-run automatic resume: restart a channel from the last checkpointed message
# id when a connection error escapes iter_messages.
RESUME_MAX_ATTEMPTS = 5    # restarts in a row without progress before re-raising
RESUME_BASE_WAIT = 30      # wait = min(RESUME_BASE_WAIT * attempt, RESUME_MAX_WAIT)
RESUME_MAX_WAIT = 300

# a dropped connection: never swallowed as a per-message skip, always bubbles up
# to the channel-level retry loop so the work is redone rather than lost.
NET_ERRORS = (ConnectionError, OSError, asyncio.TimeoutError)

# Transient server-side RPC failures (500 RPC_CALL_FAIL / RPC_MCGET_FAIL,
# 503 TIMEOUT): Telegram is telling us to retry, so treat them like a dropped
# connection — redo the whole post/channel rather than keep a half-scraped thread.
# 400 (MSG_ID_INVALID on posts with no comments) and 403 (BROADCAST_FORBIDDEN on
# channel-post reactors) are deliberately NOT here: those stay per-message skips.
RETRYABLE_RPC = (ServerError, TimedOutError)


def _progress_bar(frac: float, width: int = 20) -> str:
    frac = min(max(frac, 0.0), 1.0)
    filled = round(frac * width)
    return "█" * filled + "░" * (width - filled)


# How often (in scraped posts) to flush the in-memory buffer to a checkpoint shard.
CHECKPOINT_EVERY = 150
# Seconds between progress lines: one per post would flood journald on a days-long bot scrape
# (the bot's 📊 gets every post through on_progress regardless).
PRINT_EVERY = 5

# One (person, message, reaction) is unique; a resume overlap or a repeated
# reaction-list page can re-emit a row, so exact repeats on this key are dropped.
REACTOR_DEDUP_KEY = ["Group", "Target", "Message ID", "Reactor ID", "Reaction"]


def _atomic_parquet(df: pd.DataFrame, dest: Path) -> None:
    """Write via a temp file + rename, so an OOM-kill mid-write can't leave a
    half-written shard behind."""
    tmp = dest.with_name(dest.name + ".tmp")
    df.to_parquet(tmp, index=False)
    tmp.replace(dest)


def _atomic_write_text(dest: Path, text: str) -> None:
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(dest)


def _shard_paths(ckpt_dir: Path, base: str) -> list[Path]:
    return sorted(ckpt_dir.glob(f"{base}_part_*.parquet"))


def _shard_num(p: Path) -> int:
    return int(p.stem.rsplit("_", 1)[1])


# A post row's columns (see _collect_post) with fixed types. Each checkpoint shard is a
# DataFrame of its own, so pandas infers a column all-null in one shard and typed in
# another differently; cast to this, the shards stitch into one file chunk by chunk.
_POST_SCHEMA = pa.schema([
    ("Type", pa.string()), ("Group", pa.string()), ("Topic ID", pa.int64()), ("Author ID", pa.int64()),
    ("Author Username", pa.string()), ("Author Access Hash", pa.int64()),
    ("Author Name", pa.string()), ("Content", pa.string()), ("Date", pa.string()),
    ("Message ID", pa.int64()), ("Author", pa.string()), ("Views", pa.int64()),
    ("Reactions", pa.string()), ("Shares", pa.int64()), ("Media", pa.bool_()),
    ("Url", pa.string()), ("Comments List", pa.string()),
])
# ... and the _posts file's, as normalize_keys/normalize_values leave them
_POSTS_FILE_SCHEMA = (_POST_SCHEMA
                      .set(_POST_SCHEMA.get_field_index("Date"), pa.field("Date", pa.timestamp("us")))
                      .set(_POST_SCHEMA.get_field_index("Message ID"), pa.field("Message ID", pa.string()))
                      .append(pa.field("Comments", pa.int64(), nullable=False)))


_INT64_NULLABLE = {pa.int64(): pd.Int64Dtype()}  # to_pandas: int64 with nulls stays exact


def _pandas_schema(schema: pa.Schema, attrs: dict | None = None) -> pa.Schema:
    """schema plus the pandas metadata pd.read_parquet restores dtypes and df.attrs from:
    without it a nullable int64 column (an access hash with gaps) reads back as float64,
    rounded."""
    def dtype(field):
        if field.type == pa.int64():
            return "Int64" if field.nullable else "int64"
        if pa.types.is_timestamp(field.type):
            return f"datetime64[{field.type.unit}]"
        return "bool" if field.type == pa.bool_() else "str"

    template = pd.DataFrame({f.name: pd.Series(dtype=dtype(f)) for f in schema})
    metadata = dict(pa.Schema.from_pandas(template, preserve_index=False).metadata)
    if attrs:
        metadata[b"PANDAS_ATTRS"] = json.dumps(attrs).encode()
    return schema.with_metadata(metadata)


def _post_table(shard: Path) -> pa.Table:
    """One posts shard in _POST_SCHEMA (a shard from before a column existed gets it null)."""
    df = pd.read_parquet(shard).reindex(columns=_POST_SCHEMA.names)
    return pa.Table.from_pandas(df, schema=_POST_SCHEMA, preserve_index=False)


def _write_snapshot(ckpt_dir: Path, start: int, dest: Path) -> None:
    """The `<channel>_until_` snapshot: the posts shards with index >= start in one parquet
    file, written shard by shard (a giant channel's posts are never all in memory)."""
    tmp = dest.with_name(dest.name + ".tmp")
    schema = _pandas_schema(_POST_SCHEMA)
    with pq.ParquetWriter(tmp, schema) as writer:
        for p in _shard_paths(ckpt_dir, "posts"):
            if _shard_num(p) >= start:
                writer.write_table(_post_table(p).replace_schema_metadata(schema.metadata))
    tmp.replace(dest)


def _write_posts(ckpt_dir: Path, out_dir: Path, name: str, attrs: dict) -> tuple[Path, int, str]:
    """The `<name>_posts_<from>-<to>.parquet` output, shard by shard: memory is one shard
    plus the (group, id) keys that drop a resume overlap's repeats (the first copy is kept).
    The posts go channel by channel, newest first within each. attrs land in the file's
    metadata, where pandas reads df.attrs from. Returns (path, rows, span)."""
    from scraper.analysis import normalize_keys, normalize_values

    out_dir.mkdir(parents=True, exist_ok=True)
    schema = _pandas_schema(_POSTS_FILE_SCHEMA, attrs)
    tmp = out_dir / f"{name}_posts.parquet.tmp"
    seen: set[str] = set()
    rows, first, last = 0, None, None
    with pq.ParquetWriter(tmp, schema) as writer:
        for p in _shard_paths(ckpt_dir, "posts"):
            # nullable Int64, not float64: a 64-bit access hash must not be rounded
            df = normalize_keys(_post_table(p).to_pandas(types_mapper=_INT64_NULLABLE.get))
            fresh = []
            for key in df["Group"] + "\x00" + df["Message ID"]:
                fresh.append(key not in seen)
                seen.add(key)
            df = df[fresh]
            if df.empty:
                continue
            df = normalize_values(df)
            first = min(first, df["Date"].min()) if first is not None else df["Date"].min()
            last = max(last, df["Date"].max()) if last is not None else df["Date"].max()
            writer.write_table(pa.Table.from_pandas(df, schema=schema, preserve_index=False))
            rows += len(df)
    span = f"_{first:%d.%m.%Y}-{last:%d.%m.%Y}" if rows else ""
    dest = out_dir / f"{name}_posts{span}.parquet"
    tmp.replace(dest)  # atomic: no half-written output if this step is killed
    return dest, rows, span


def _consolidate_reactors(ckpt_dir: Path, dest: Path) -> tuple[Path, int]:
    """Stream the reactor shards into one parquet file, deduplicating without ever
    holding the whole table:
      * within a shard — on the full row key (a repeated reaction-list page);
      * across shards — at message level: every reactor row of a message is written
        to a single shard (a message's rows are appended together, before the
        `t_index % CHECKPOINT_EVERY` flush), so the same (Group, Target, Message ID) turning
        up in a later shard is a resume-overlap re-scrape and its copy is dropped.
    Memory: O(distinct reacted messages) for the seen-set, never O(reactor rows).

    Caller guarantees at least one non-empty reactor shard, so `writer` is always
    created (every shard is written from a non-empty buffer)."""
    seen: set = set()
    tmp = dest.with_name(dest.name + ".tmp")
    writer = None
    total = 0
    try:
        for p in _shard_paths(ckpt_dir, "reactors"):
            part = pd.read_parquet(p).drop_duplicates(subset=REACTOR_DEDUP_KEY)
            msg_keys = list(zip(part["Group"].astype(str), part["Target"], part["Message ID"]))
            fresh = [k not in seen for k in msg_keys]
            part = part[fresh]
            if part.empty:
                continue
            seen.update(k for k, f in zip(msg_keys, fresh) if f)
            table = pa.Table.from_pandas(part, preserve_index=False)
            if writer is None:
                writer = pq.ParquetWriter(tmp, table.schema)
            else:
                table = table.cast(writer.schema)
            writer.write_table(table)
            total += len(part)
    finally:
        if writer is not None:
            writer.close()
    tmp.replace(dest)  # atomic: no half-written final file if this step is killed
    return dest, total


def _count_shard_rows(ckpt_dir: Path, base: str) -> int:
    """Row total across `<base>` shards, from parquet footer metadata only — no row
    data is read into memory."""
    return sum(pq.ParquetFile(p).metadata.num_rows for p in _shard_paths(ckpt_dir, base))


def _next_shard_index(ckpt_dir: Path) -> int:
    idxs = [_shard_num(p)
            for base in ("posts", "reactors")
            for p in _shard_paths(ckpt_dir, base)]
    return max(idxs) + 1 if idxs else 0


def _clear_checkpoint(ckpt_dir: Path) -> None:
    """Remove a previous job's checkpoint artefacts: a fresh (non-resume) run under
    the same name must not inherit stale shards, and a clean finish leaves nothing
    behind."""
    if not ckpt_dir.exists():
        return
    for p in (*ckpt_dir.glob("*.parquet"), *ckpt_dir.glob("*.tmp"),
              *ckpt_dir.glob("*.session"), *ckpt_dir.glob("*.session-journal")):  # entity cache
        p.unlink()
    (ckpt_dir / "resume.json").unlink(missing_ok=True)


@dataclass
class ScrapeParams:
    channels: list[str]
    date_min: datetime
    date_max: datetime
    name: str
    keyword: str = ""
    max_messages: int = 1_000_000
    out_dir: Path = Path("output")
    with_comments: bool = True
    with_reactors: bool = True
    with_participants: bool = True
    resume: bool = False  # continue from <out_dir>/<name>_partial/checkpoint (see pending_resume)
    # the account's session path: kept in the checkpoint, a resume must run on the same
    # account (the scraped access hashes are valid for it only)
    account: str = ""
    # set from another thread to stop the run: it checkpoints and exits like a Ctrl-C
    stop: threading.Event | None = None
    # set by the run: the account that scraped; its access hashes are the only valid ones
    owner_id: int | None = None
    # called with (overall fraction, ETA seconds or None, posts so far) on each post: the bot's 📊 button
    on_progress: Callable[[float, float | None, int], None] | None = None


def pending_resume(out_dir: str | Path, name: str) -> dict | None:
    """The interrupted scrape's resume.json under <out_dir>/<name>_partial, or None."""
    rj = Path(out_dir) / f"{name}_partial" / "checkpoint" / "resume.json"
    try:
        return json.loads(rj.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # none, or a half-written / foreign file: nothing to continue
        return None


def resume_params(meta: dict, out_dir: str | Path) -> ScrapeParams:
    """ScrapeParams that continue the scrape `meta` (from pending_resume) as it was started.
    A checkpoint written before the settings were stored gets the defaults."""
    defaults = ScrapeParams.__dataclass_fields__
    return ScrapeParams(
        channels=list(meta["channels"]),
        date_min=datetime.fromisoformat(meta["date_min"]),
        date_max=datetime.fromisoformat(meta["date_max"]),
        name=meta["name"],
        keyword=meta.get("keyword", ""),
        max_messages=meta.get("max_messages", defaults["max_messages"].default),
        out_dir=Path(out_dir),
        with_comments=meta.get("with_comments", True),
        with_reactors=meta.get("with_reactors", True),
        with_participants=meta.get("with_participants", True),
        resume=True,
        account=meta.get("account", ""),
    )


def channel_slug(channel: str) -> str:
    """Reduce '@name', 't.me/name', 'https://t.me/name/123?x=1' etc. to a bare 'name'."""
    s = channel.strip()
    for prefix in ("https://", "http://"):  # scheme and domain ignore case; the name keeps it
        if s.lower().startswith(prefix):
            s = s[len(prefix):]
    if s.lower().startswith("www."):
        s = s[len("www."):]
    for prefix in ("t.me/", "telegram.me/", "telegram.dog/"):
        if s.lower().startswith(prefix):
            s = s[len(prefix):]
    if s.startswith("s/"):  # web preview of the channel
        s = s[len("s/"):]
    elif s.startswith("joinchat/"):  # legacy invite, same chat as t.me/+<hash>
        s = "+" + s[len("joinchat/"):]
    s = s.split("?")[0].split("#")[0]
    s = s.strip("/").split("/")[0]
    return s.lstrip("@")


def parse_channels(raw: str) -> list[str]:
    """Split a comma/whitespace-separated channel list, dropping blanks."""
    return [c.strip() for c in re.split(r"[,\s]+", raw) if c.strip()]


class _ChannelRef(NamedTuple):
    arg: str | int   # what to pass to Telethon: int for a numeric ID, "@name" for a t.me/s/ link,
                     # a t.me/+hash URL for an invite, else the raw string; the scrape loop
                     # replaces it with the resolved InputPeer
    slug: str        # Group column value + output filename component
    url_base: str    # a message URL is f"{url_base}/{message_id}"; "" for a basic group or a
                     # private chat, whose messages have no links (see _url)
    topic: int | None = None  # a forum topic to scrape alone (see _resolve_topic)
    forum: bool = False       # the chat is a forum: each post gets its Topic ID


def _url(ref: _ChannelRef, post_id: int, comment_id: int | None = None) -> str:
    if not ref.url_base:
        return ""
    url = f"{ref.url_base}/{post_id}"
    return f"{url}?comment={comment_id}" if comment_id is not None else url


def _link_topic(raw: str) -> int | None:
    """The number after the chat in a t.me link (t.me/<name>/N, t.me/c/<id>/N): a forum topic,
    or a post - only the resolved chat tells (see _resolve_topic)."""
    m = re.match(r"(?i:(?:https?://)?(?:www\.)?(?:t\.me|telegram\.(?:me|dog)))/(?:s/)?"
                 r"(?:c/\d+|(?!c/)[A-Za-z0-9_]+)/(\d+)", raw.strip())
    return int(m[1]) if m else None


def _channel_ref(raw: str) -> _ChannelRef:
    """Resolve a user-supplied channel (@name / t.me URL / numeric ID) to how we
    address it and how we render it. Numeric IDs (e.g. '-1001629147115', as shown
    by Telegram clients) become `t.me/c/<short_id>` links and a 'c<short_id>' slug."""
    s = raw.strip()
    topic = _link_topic(s)
    m = re.match(r"(?i:(?:https?://)?(?:www\.)?(?:t\.me|telegram\.(?:me|dog)))/c/(\d+)", s)
    if m:  # a private-channel link: the same channel as its -100<id> numeric ID
        s = f"-100{m[1]}"
    elif s.isdigit():  # a bare short id, as in t.me/c/<id>: the same channel as -100<id>
        s = f"-100{s}"
    if s.startswith("-") and s[1:].isdigit():
        cid = int(s)
        short, peer_type = utils.resolve_id(cid)  # -100… marker -> bare id
        if peer_type is PeerChat:  # a basic (legacy) group: -<id>, no t.me/c/ links
            return _ChannelRef(cid, f"chat{short}", "", topic)
        return _ChannelRef(cid, f"c{short}", f"https://t.me/c/{short}", topic)
    name = channel_slug(s)
    # an invite resolves only as a t.me/+<hash> URL; anything else goes as "@name", since
    # Telethon can't parse a t.me/s/<name> preview, a post link or a URL with a query
    arg = f"https://t.me/{name}" if name.startswith("+") else f"@{name}"
    return _ChannelRef(arg, name, f"https://t.me/{name}", topic)


async def _resolve_topic(client, ref: _ChannelRef, entity) -> tuple[_ChannelRef, str | None]:
    """Settle the link's number: a topic only in a forum that has it, else the whole chat (in a
    channel the number is a post). A topic's posts get their own Group "@<slug>-topic<N>" (a
    username has no "-"). Returns (ref, the topic's title)."""
    if not getattr(entity, "forum", False):
        return ref._replace(topic=None), None
    ref = ref._replace(forum=True)
    if ref.topic is None:
        return ref, None
    res = await client(GetForumTopicsByIDRequest(peer=ref.arg, topics=[ref.topic]))
    topic = next((t for t in res.topics if isinstance(t, ForumTopic) and t.id == ref.topic), None)
    if topic is None:  # a link to a message, not to a topic: the whole forum
        return ref._replace(topic=None), None
    return ref._replace(slug=f"{ref.slug}-topic{ref.topic}"), topic.title


def _topic_id(message, forum: bool) -> int | None:
    """A forum message's topic: its reply header names it, none means General (1)."""
    if not forum:
        return None
    header = getattr(message, "reply_to", None)
    if header is not None and getattr(header, "forum_topic", False):
        return header.reply_to_top_id or header.reply_to_msg_id
    return 1


def group_channel(group: str) -> str:
    """The channel to pass back to `_channel_ref` for a Group column value ("@<slug>")."""
    slug = group.lstrip("@")
    chat, sep, topic = slug.rpartition("-topic")
    if sep and topic.isdigit() and not chat.startswith("+"):  # a forum topic: its link
        if chat[:1] == "c" and chat[1:].isdigit():
            return f"https://t.me/c/{chat[1:]}/{topic}"
        return f"https://t.me/{chat}/{topic}"
    if slug.startswith("chat") and slug[4:].isdigit():  # a basic group given by numeric id
        return f"-{slug[4:]}"
    if slug[:1] == "c" and slug[1:].isdigit():  # a channel given by numeric id
        return f"-100{slug[1:]}"
    if slug.startswith("+"):  # an invite: resolves only as a t.me/+<hash> URL
        return f"https://t.me/{slug}"
    return f"@{slug}"


async def _warm_channel(client, ref: _ChannelRef, dialogs_loaded: bool) -> bool:
    """A channel given by numeric ID resolves only from the session's entity cache
    (no username to look up). A fresh or string session has none, so on a miss load
    the dialog list: its entities land in the cache, as for any response. Returns
    whether the dialogs are loaded, so a run does it at most once."""
    if dialogs_loaded or not isinstance(ref.arg, int):
        return dialogs_loaded
    try:
        await client.get_input_entity(ref.arg)
    except (ValueError, ChannelPrivateError):  # uncached; Telethon's access_hash=0 probe failed
        await client.get_dialogs()
        return True
    return False


async def _reconnect(client) -> None:
    try:
        if not client.is_connected():
            await client.connect()  # Telethon won't recover a hard _disconnect on its own
    except Exception as ce:
        print(f"  ! reconnect failed: {ce}")


def check_date_range(date_min: datetime, date_max: datetime, raw_min: str, raw_max: str) -> None:
    """SystemExit on a reversed range: it would scrape / verify nothing, silently."""
    if date_min > date_max:
        raise SystemExit(f"The start date {raw_min} is after the end date {raw_max}.")


def parse_date(value: str, *, end_of_day: bool = False) -> datetime:
    """Accept 'DD.MM.YYYY' or an ISO date ('YYYY-MM-DD')."""
    value = value.strip()
    try:  # a date only: end_of_day extends it; an explicit time is taken as given
        try:
            d = date.fromisoformat(value)
        except ValueError:
            d = datetime.strptime(value, "%d.%m.%Y").date()
        dt = datetime.combine(d, dtime(23, 59, 59) if end_of_day else dtime())
    except ValueError:
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            raise SystemExit(f"Bad date {value!r}: use DD.MM.YYYY or YYYY-MM-DD")
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _reaction_emoji(reaction) -> str:
    """Human-readable label for one Reaction: the emoji, '[custom:<id>]' or '[stars]'."""
    emoji = getattr(reaction, "emoticon", None)
    if emoji is None:
        doc_id = getattr(reaction, "document_id", None)
        emoji = f"[custom:{doc_id}]" if doc_id is not None else "[stars]"
    return emoji


def _reactions_to_string(reactions) -> str:
    if not reactions:
        return ""
    parts = [f"{_reaction_emoji(r.reaction)} {r.count}" for r in reactions.results]
    return " ".join(parts) + (" " if parts else "")


def _sender_username(msg) -> str:
    """Username of a message's sender. '' for a user without one; a marker when the
    sender is a channel (anonymous admin / linked channel) or is unavailable."""
    sender = getattr(msg, "sender", None)
    if sender is None:
        return "[anonymous]"
    if isinstance(sender, User):
        return sender.username or ""
    return "[channel]"


def _user_access_hash(entity) -> int | None:
    """access_hash of a full User; None otherwise. A "min" User's hash is not
    usable in InputPeerUser, so it counts as missing; a bot or a deleted account has
    no one to mail, so it is left out of the participants base the same way."""
    if isinstance(entity, User) and not entity.min and not entity.bot and not entity.deleted:
        return entity.access_hash
    return None


def _sender_access_hash(msg) -> int | None:
    return _user_access_hash(getattr(msg, "sender", None))


def _entity_name(entity) -> str:
    """Display name: a User's first + last name, else a Channel/Chat title; '' if unknown."""
    if entity is None:
        return ""
    name = " ".join(
        p for p in (getattr(entity, "first_name", None), getattr(entity, "last_name", None)) if p
    )
    return name or getattr(entity, "title", "") or ""


def _sender_name(msg) -> str:
    return _entity_name(getattr(msg, "sender", None))


async def _collect_reactors(client, peer, ref: _ChannelRef, post_id: int, msg, target: str) -> list[dict]:
    """Per-user reaction list for one message (`target` is 'post' or 'comment').

    Telegram refuses this for broadcast-channel posts (BroadcastForbiddenError); like
    _collect_comments, any RPC error is logged and that message is skipped. When the
    message's reactions carry can_see_list=False, Telegram has already said the list
    is hidden, so the doomed request is skipped before it is made.
    """
    reactions = getattr(msg, "reactions", None)
    if not reactions or getattr(reactions, "can_see_list", None) is False:
        return []  # nothing reacted, or Telegram hides the list -> no API call
    url = _url(ref, post_id, msg.id if target == "comment" else None)
    rows: list[dict] = []
    offset = None
    try:
        while True:
            res = await client(
                GetMessageReactionsListRequest(peer=peer, id=msg.id, limit=100, offset=offset)
            )
            await asyncio.sleep(REACTOR_CALL_DELAY)
            # separate maps: a user and a channel can share the same bare id
            users = {u.id: u for u in res.users}
            chats = {c.id: c for c in res.chats}
            for pr in res.reactions:
                peer_id = pr.peer_id
                eid = (
                    getattr(peer_id, "user_id", None)
                    or getattr(peer_id, "channel_id", None)
                    or getattr(peer_id, "chat_id", None)
                )
                ent = (users if isinstance(peer_id, PeerUser) else chats).get(eid)
                if isinstance(peer_id, PeerUser):
                    rid, uname = peer_id.user_id, (getattr(ent, "username", "") or "")
                    ahash = _user_access_hash(ent)
                else:  # PeerChannel / PeerChat
                    rid, uname, ahash = utils.get_peer_id(peer_id), "[channel]", None
                rows.append(
                    {
                        "Type": "reactor",
                        "Target": target,
                        "Group": f"@{ref.slug}",
                        "Message ID": msg.id,
                        "Post ID": post_id,
                        "Url": url,
                        "Reactor ID": rid,
                        "Reactor Username": uname,
                        "Reactor Access Hash": ahash,
                        "Reactor Name": _entity_name(ent),
                        "Reaction": _reaction_emoji(pr.reaction),
                        "Date": pr.date.strftime("%Y-%m-%d %H:%M:%S") if pr.date else "",
                    }
                )
            if not res.next_offset:
                break
            offset = res.next_offset
    except (*NET_ERRORS, FloodWaitError, *RETRYABLE_RPC):  # disconnect, soft ban, or a
        raise                             # transient 500/503: redo the post, don't skip
    except Exception as exc:  # BroadcastForbidden, thread removed, ...
        print(f"  ! reactors for {ref.slug}/{post_id} ({target} {msg.id}): {exc}")
    return rows


async def _collect_comments(
    client, ref: _ChannelRef, message, *, with_reactors: bool = False
) -> tuple[list[dict], list[dict]]:
    """Replies to one post that has a linked discussion thread.

    Returns (comments, reactor_rows); reactor_rows is empty unless with_reactors.
    """
    comments: list[dict] = []
    reactors: list[dict] = []
    # The linked discussion group: authoritative for where the replies live.
    # (message.input_chat / c.input_chat point back at the broadcast channel when
    # iterating with reply_to=, so they can't be trusted for the reactions call.)
    disc_id = getattr(message.replies, "channel_id", None)
    disc_peer = PeerChannel(disc_id) if disc_id else None
    try:
        async for c in client.iter_messages(ref.arg, reply_to=message.id):
            if isinstance(c, MessageService):  # e.g. a pin in the thread: not a comment
                continue
            comments.append(
                {
                    "Type": "comment",
                    "Comment Group": f"@{ref.slug}",
                    "Comment Author ID": c.sender_id,
                    "Comment Author Username": _sender_username(c),
                    "Comment Author Access Hash": _sender_access_hash(c),
                    "Comment Author Name": _sender_name(c),
                    "Comment Content": c.text or "",
                    "Comment Date": c.date.strftime("%Y-%m-%d %H:%M:%S"),
                    "Comment Message ID": c.id,
                    "Comment Author": c.post_author,
                    "Comment Views": c.views,
                    "Comment Reactions": _reactions_to_string(c.reactions),
                    "Comment Shares": c.forwards,
                    "Comment Media": bool(c.media),
                    "Comment Url": _url(ref, message.id, c.id),
                }
            )
            if with_reactors:
                peer = disc_peer or getattr(c, "input_chat", None) or ref.arg
                reactors += await _collect_reactors(client, peer, ref, message.id, c, "comment")
    except (*NET_ERRORS, FloodWaitError, *RETRYABLE_RPC):  # disconnect, soft ban, or a
        raise                             # transient 500/503: redo the post, don't skip
    except Exception as exc:  # thread just removed, ...
        print(f"  ! comments for {ref.slug}/{message.id}: {exc}")
    return comments, reactors


async def _collect_post(client, ref: _ChannelRef, message, params: ScrapeParams) -> tuple[dict, list[dict]]:
    """One post row plus its reactor rows (comment reactors first, then post reactors)."""
    # a 0 comment count is trusted only once the post has had time to settle
    fresh = datetime.now(timezone.utc) - message.date < RECENT_POST_WINDOW
    has_thread = bool(
        message.replies and message.replies.comments
        and (message.replies.replies or fresh)
    )
    comments, reactors = (
        await _collect_comments(client, ref, message, with_reactors=params.with_reactors)
        if params.with_comments and has_thread
        else ([], [])
    )
    if params.with_reactors:
        peer = getattr(message, "input_chat", None) or ref.arg
        reactors += await _collect_reactors(client, peer, ref, message.id, message, "post")
    row = {
        "Type": "text",
        "Group": f"@{ref.slug}",
        "Topic ID": _topic_id(message, ref.forum),
        "Author ID": message.sender_id,
        # a group's people are its message authors (a channel's posts are the channel's own)
        "Author Username": _sender_username(message),
        "Author Access Hash": _sender_access_hash(message),
        "Author Name": _sender_name(message),
        "Content": clean_xml_text(message.text),
        "Date": message.date.strftime("%Y-%m-%d %H:%M:%S"),
        "Message ID": message.id,
        "Author": message.post_author,
        "Views": message.views,
        "Reactions": _reactions_to_string(message.reactions),
        "Shares": message.forwards,
        "Media": bool(message.media),
        "Url": _url(ref, message.id),
        "Comments List": clean_xml_text(json.dumps(comments, ensure_ascii=False)),
    }
    return row, reactors


async def _scrape(creds: Credentials, params: ScrapeParams) -> None:
    params.out_dir.mkdir(parents=True, exist_ok=True)
    # per-channel history snapshots go in partial_dir; the resume machinery goes one
    # level down in ckpt_dir, so combining <name>_partial (analysis) only sees the snapshots
    partial_dir = params.out_dir / f"{params.name}_partial"
    ckpt_dir = partial_dir / "checkpoint"

    data: list[dict] = []
    reactors: list[dict] = []
    failed: list[tuple[str, str]] = []  # (channel, error) of channels cut short by an error
    t_index = 0
    start_time = time.monotonic()
    printed_at = None  # elapsed time of the last progress line

    n_channels = len(params.channels)
    span = (params.date_max - params.date_min).total_seconds()

    def _date_frac(msg_date) -> float:
        if span <= 0:
            return 1.0
        return min(max((params.date_max - msg_date).total_seconds() / span, 0.0), 1.0)

    shard_index = 0

    def _write_checkpoint(channel_index: int, last_id: int) -> None:
        nonlocal shard_index
        if not data and not reactors and not (ckpt_dir / "resume.json").exists():
            return  # nothing scraped yet and no cursor to advance
        ckpt_dir.mkdir(parents=True, exist_ok=True)
        # append-only: write ONLY the rows gathered since the previous checkpoint to
        # a fresh shard, then free them. Earlier shards are never reread or rewritten,
        # so a checkpoint costs O(batch) regardless of run size. Always parquet:
        # lossless and format-stable for a resume.
        wrote = False
        if data:
            pdf = pd.DataFrame(data)
            # int64 + None would become float64 and corrupt the hash; keep a stable type
            pdf["Author Access Hash"] = pd.array(
                [r["Author Access Hash"] for r in data], dtype="Int64")
            pdf["Topic ID"] = pd.array([r["Topic ID"] for r in data], dtype="Int64")
            _atomic_parquet(pdf, ckpt_dir / f"posts_part_{shard_index:05}.parquet")
            data.clear()
            wrote = True
        if reactors:
            rdf = pd.DataFrame(reactors)
            # int64 + None would become float64 and corrupt the hash; keep a stable type
            rdf["Reactor Access Hash"] = pd.array(
                [r["Reactor Access Hash"] for r in reactors], dtype="Int64")
            _atomic_parquet(rdf,
                            ckpt_dir / f"reactors_part_{shard_index:05}.parquet")
            reactors.clear()
            wrote = True
        if wrote:
            shard_index += 1
        _atomic_write_text(ckpt_dir / "resume.json", json.dumps({
            "name": params.name,
            "channels": params.channels,
            "keyword": params.keyword,
            "date_min": params.date_min.isoformat(),
            "date_max": params.date_max.isoformat(),
            # the run's settings too, so a resume continues it exactly (see resume_params)
            "account": params.account,
            "max_messages": params.max_messages,
            "with_comments": params.with_comments,
            "with_reactors": params.with_reactors,
            "with_participants": params.with_participants,
            "channel_index": channel_index,
            "last_id": last_id,
            "t_index": t_index,
            "failed": failed,
            "updated": datetime.now(timezone.utc).isoformat(),
        }, indent=2))

    resume_channel_index = 0
    resume_last_id = 0
    resume_meta_ok = params.resume and (ckpt_dir / "resume.json").exists()
    if params.resume and not resume_meta_ok:
        print(f"  ! resume: {ckpt_dir / 'resume.json'} not found; starting a fresh run")
    if not resume_meta_ok:
        _clear_checkpoint(ckpt_dir)  # fresh run: never inherit a previous job's shards
        for p in partial_dir.glob("*_until_*"):  # ... or its per-channel snapshots
            p.unlink()
    else:
        rj = ckpt_dir / "resume.json"
        meta = json.loads(rj.read_text(encoding="utf-8"))
        if (meta.get("channels") != params.channels
                or meta.get("keyword", "") != params.keyword
                or meta.get("date_min") != params.date_min.isoformat()
                or meta.get("date_max") != params.date_max.isoformat()):
            raise SystemExit("resume: resume.json does not match the current "
                             "channels / keyword / dates of the interrupted scrape.")
        if not _shard_paths(ckpt_dir, "posts") and int(meta.get("t_index", 0)) > 0:
            raise SystemExit(f"resume: checkpoint shards are missing from {ckpt_dir} "
                             f"but resume.json reports {meta['t_index']} scraped posts "
                             f"— cannot resume safely.")
        shard_index = _next_shard_index(ckpt_dir)
        t_index = _count_shard_rows(ckpt_dir, "posts")  # footer metadata only, no data
        n_reactor_rows = _count_shard_rows(ckpt_dir, "reactors")
        resume_channel_index = int(meta["channel_index"])
        resume_last_id = int(meta["last_id"])
        failed = [tuple(f) for f in meta.get("failed", [])]
        print(SEP)
        print(f"Resuming '{params.name}': channel {resume_channel_index + 1}/{n_channels}, "
              f"{t_index} posts + {n_reactor_rows} reactor rows already saved in "
              f"{shard_index} shard(s), continuing from id {resume_last_id or 'newest'}")
        print(SEP)

    ckpt_dir.mkdir(parents=True, exist_ok=True)
    # before connecting: ⏹ must work while a dead proxy is retried for hours
    watcher = watch_stop(params.stop)
    client = None
    try:
        client = await connect(creds, entity_cache=ckpt_dir / "entities")
        params.owner_id = (await client.get_me()).id
    except BaseException:  # a stop lands in run(), which reports it like any interruption
        if watcher is not None:
            watcher.cancel()
        if client is not None:
            await client.disconnect()
        raise

    i, last_id = resume_channel_index, resume_last_id  # for the Ctrl-C handler below
    snapshot_from = 0  # first shard index not yet written to an `_until_` snapshot
    dialogs_loaded = False
    channel_closed = False  # the channel's final checkpoint is written; Ctrl-C must not roll it back
    cut_short: int | None = None  # first channel the max-messages stop left unfinished

    # SIGTERM (a terminal closed, `kill`) is NOT turned into a KeyboardInterrupt the way
    # SIGINT is, so without this it would kill the run mid-batch. Convert it to task
    # cancellation so the CancelledError handler below checkpoints a clean resume point.
    # Only in the main thread (the terminal menu); when the bot runs a scrape in a worker thread
    # add_signal_handler is unavailable, and the bot stops it through params.stop instead.
    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()
    sigterm_handled = False
    # the bot's child process has its own (SIGTERM -> stop): handed back after the loop, as
    # remove_signal_handler would leave SIG_DFL, killing the final file writes on a SIGTERM
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    if threading.current_thread() is threading.main_thread():
        try:
            loop.add_signal_handler(signal.SIGTERM, main_task.cancel)
            sigterm_handled = True
        except (NotImplementedError, RuntimeError, ValueError):
            pass  # Windows or a loop without signal support: fall back to per-150 checkpoints
    try:
        for i, channel in enumerate(params.channels):
            if i < resume_channel_index:
                continue
            if t_index >= params.max_messages:
                if cut_short is None:
                    cut_short = i
                break

            loop_start = time.monotonic()
            c_index = 0
            last_id = resume_last_id if i == resume_channel_index else 0
            attempt = 0
            flood_attempts = 0
            flood_last_id = None  # cursor at the previous FloodWaitError
            net_last_id = None    # cursor at the previous dropped connection
            done_channel = False
            channel_closed = False
            try:
                ref = _channel_ref(channel)
                dialogs_loaded = await _warm_channel(client, ref, dialogs_loaded)
                # resolve once: a t.me/+hash string misses the session cache, so every
                # iter_messages would otherwise re-check the invite over the network
                ref = ref._replace(arg=await client.get_input_entity(ref.arg))
                if ref.slug.startswith("+") and isinstance(ref.arg, InputPeerChannel):
                    # t.me/+hash/<id> is no message link; the chat's t.me/c/<id>/<id> is
                    ref = ref._replace(url_base=f"https://t.me/c/{ref.arg.channel_id}")
                elif isinstance(ref.arg, (InputPeerChat, InputPeerUser)):
                    # an invite into a basic group, or a private chat: no message links
                    ref = ref._replace(url_base="")
                try:
                    entity = await client.get_entity(ref.arg)
                except Exception:
                    entity = None
                title = getattr(entity, "title", None)
                ref, topic_title = await _resolve_topic(client, ref, entity)
                label = f'"{title}" ({channel})' if title else channel
                if topic_title:
                    label += f' topic "{topic_title}"'
                # one topic is its reply thread; the General topic has none: the whole chat,
                # filtered below. Telethon ignores search= with reply_to=, so the keyword is
                # matched locally for a topic.
                thread = {"reply_to": ref.topic} if ref.topic not in (None, 1) else {}
                search = None if ref.topic is not None else params.keyword or None
                keyword = params.keyword.lower()
                print(f"=== ch {i + 1}/{n_channels}: {label} ===")
                # progress is measured over the message-id range in [date_min, date_max]:
                # two cheap limit=1 fetches give the newest and the just-below-floor ids.
                try:
                    _hi = await client.get_messages(ref.arg, limit=1, offset_date=params.date_max, **thread)
                    _lo = await client.get_messages(ref.arg, limit=1, offset_date=params.date_min, **thread)
                    id_hi = _hi[0].id if _hi else 0
                    id_lo = _lo[0].id if _lo else 0
                except Exception as exc:  # never let a progress probe kill the run
                    print(f"  ! progress probe failed ({exc}); ETA will be approximate")
                    id_hi = id_lo = 0
                id_span = id_hi - id_lo
                sess_start_id = last_id or id_hi  # last_id = resume cursor, else 0
                while True:
                    try:
                        async for message in client.iter_messages(
                                ref.arg, search=search, offset_id=last_id,
                                # exclusive, hence +1s; a resume's last_id is older anyway
                                offset_date=params.date_max + timedelta(seconds=1), **thread):
                            if message.date < params.date_min:
                                done_channel = True
                                break
                            if message.date > params.date_max:
                                continue
                            if isinstance(message, MessageService):  # pin, photo change, joins: not a post
                                last_id = message.id  # still progress: retries resume below it
                                continue
                            if ref.topic is not None and (
                                    _topic_id(message, True) != ref.topic
                                    or keyword not in (message.text or "").lower()):
                                last_id = message.id  # another topic, or no keyword
                                continue

                            row, row_reactors = await _collect_post(client, ref, message, params)
                            data.append(row)
                            reactors.extend(row_reactors)
                            c_index += 1
                            t_index += 1
                            last_id = message.id
                            date_str = row["Date"]

                            now = time.monotonic() - start_time
                            chan_elapsed = time.monotonic() - loop_start  # sess_ids is per channel too
                            if id_span > 0:                                # current channel fraction
                                cf = min(max((id_hi - message.id) / id_span, 0.0), 1.0)
                            else:
                                cf = _date_frac(message.date)             # probe failed: fall back
                            overall = (i + cf) / n_channels
                            sess_ids = sess_start_id - message.id         # ids this process consumed
                            eta_s = (None
                                     if not (id_span > 0 and sess_ids > 0 and chan_elapsed > 30)
                                     else max(message.id - id_lo, 0) * chan_elapsed / sess_ids)
                            eta = "estimating" if eta_s is None else format_duration(eta_s)
                            if params.on_progress is not None:
                                params.on_progress(overall, eta_s, t_index)
                            if printed_at is None or now - printed_at >= PRINT_EVERY:
                                printed_at = now
                                print(
                                    f"|{_progress_bar(overall)}| {overall * 100:5.1f}%  "
                                    f"ch {i + 1}/{n_channels} ({cf * 100:3.0f}%) "
                                    f"| {c_index:05} here / {t_index:05} total | id {message.id} | {date_str} "
                                    f"| elapsed {format_duration(now)} | ETA {eta}"
                                )

                            if t_index % CHECKPOINT_EVERY == 0:
                                _write_checkpoint(i, last_id)
                                print(f"  -> checkpoint: {t_index} posts")

                            if t_index >= params.max_messages:
                                break
                        else:
                            done_channel = True
                    except FloodWaitError as exc:  # a soft ban longer than FLOOD_SLEEP_THRESHOLD
                        if last_id != flood_last_id:  # progress since the last ban: count afresh
                            flood_attempts = 0
                        flood_last_id = last_id
                        flood_attempts += 1
                        _write_checkpoint(i, last_id)
                        wait = exc.seconds + FLOOD_RETRY_BUFFER
                        if wait > FLOOD_MAX_WAIT or flood_attempts > FLOOD_MAX_ATTEMPTS:
                            print(f"  ! {label}: FLOOD_WAIT {exc.seconds}s - giving up "
                                  f"(checkpoint saved at {t_index} posts)")
                            raise
                        print(f"  ! {label}: FLOOD_WAIT {exc.seconds}s - checkpoint saved at "
                              f"{t_index} posts, waiting it out "
                              f"({flood_attempts}/{FLOOD_MAX_ATTEMPTS}), then resuming from id "
                              f"{last_id or 'newest'}")
                        await asyncio.sleep(wait)
                        await _reconnect(client)
                        continue
                    except (*NET_ERRORS, *RETRYABLE_RPC) as exc:
                        if last_id != net_last_id:  # progress since the last drop: count afresh
                            attempt = 0
                        net_last_id = last_id
                        attempt += 1
                        _write_checkpoint(i, last_id)
                        if attempt > RESUME_MAX_ATTEMPTS:
                            print(f"  ! {label}: {exc} - giving up after {attempt - 1} retries")
                            raise
                        wait = min(RESUME_BASE_WAIT * attempt, RESUME_MAX_WAIT)
                        print(f"  ! {label}: {exc} - checkpoint saved at {t_index} posts, "
                              f"retry {attempt}/{RESUME_MAX_ATTEMPTS} from id "
                              f"{last_id or 'newest'} in {wait}s")
                        await asyncio.sleep(wait)
                        await _reconnect(client)
                        continue
                    break  # iter_messages finished without a disconnect -> channel done
                if not done_channel:  # only the max-messages break gets here
                    cut_short = i

                print(f"##### {label}: done, {c_index:05} posts | "
                      f"overall {((i + 1) / n_channels) * 100:.0f}% #####")
                # flush the tail buffer to a shard and advance the resume cursor
                # (on every exit path from the channel, not just a clean finish)
                _write_checkpoint(i + 1 if done_channel else i,
                                  0 if done_channel else last_id)
                channel_closed = True
                partial_dir.mkdir(exist_ok=True)
                partial = partial_dir / f"{ref.slug}_until_{t_index:05}"
                # only this channel's new shards (after a resume the first one also
                # repeats the earlier channels' checkpointed posts; combine drops them)
                _write_snapshot(ckpt_dir, snapshot_from, partial.with_name(partial.name + ".parquet"))
                snapshot_from = shard_index
            except (*NET_ERRORS, FloodWaitError, *RETRYABLE_RPC):  # bubble to the Ctrl-C/finally scope and out to run()
                raise
            except Exception as exc:
                print(f"{channel} error: {exc}")
                if not channel_closed:  # else only the _until_ snapshot failed; the data is saved
                    failed.append((channel, f"{type(exc).__name__}: {exc}"))
                    # move past it like a finished channel, so Ctrl-C/a resume don't retry it
                    _write_checkpoint(i + 1, 0)
                    if shard_index > snapshot_from:  # rows scraped before the error: its own snapshot
                        _write_snapshot(ckpt_dir, snapshot_from,
                                        partial_dir / f"{ref.slug}_until_{t_index:05}.parquet")
                        snapshot_from = shard_index
                    channel_closed = True

            # be gentle: at least 60s per channel
            spent = time.monotonic() - loop_start
            if (spent < 60 and i < len(params.channels) - 1
                    and t_index < params.max_messages):
                await asyncio.sleep(60 - spent)
    except (KeyboardInterrupt, asyncio.CancelledError):
        # asyncio.run() turns a SIGINT into task cancellation, i.e. a
        # CancelledError raised at the current await - not KeyboardInterrupt - so
        # both must be caught here for Ctrl-C to checkpoint. (SIGTERM is turned into the
        # same cancellation by the handler installed above.)
        print()
        if not channel_closed:
            _write_checkpoint(i, last_id)
        raise
    finally:
        if watcher is not None:
            watcher.cancel()
        if sigterm_handled:
            try:
                loop.remove_signal_handler(signal.SIGTERM)
                signal.signal(signal.SIGTERM, previous_sigterm)
            except (NotImplementedError, RuntimeError, ValueError):
                pass
        await client.disconnect()

    # reached only on a run that finished without an exception (a crash bubbles to
    # run() before this and prints the resume hint); run() writes the outputs from the
    # checkpoint shards, one shard at a time.
    print(SEP)
    print(f"Concluded: {t_index:05} posts scraped")
    print(SEP)
    if failed:  # easy to miss mid-log on a long run; the checkpoint is gone after this
        print(f"  ! {len(failed)} channel(s) stopped on an error and are incomplete - "
              f"re-scrape them separately:")
        for channel, err in failed:
            print(f"    {channel}: {err}")
    if cut_short is not None:
        print(f"  ! stopped by max-messages - not scraped or cut short: "
              f"{', '.join(params.channels[cut_short:])}")



def run(creds: Credentials, params: ScrapeParams) -> Path:
    from scraper.analysis import participants

    ckpt_dir = params.out_dir / f"{params.name}_partial" / "checkpoint"
    rj = ckpt_dir / "resume.json"
    try:
        asyncio.run(_scrape(creds, params))
    except (*NET_ERRORS, FloodWaitError, KeyboardInterrupt,
            asyncio.CancelledError, *RETRYABLE_RPC) as exc:
        detail = f": {exc}" if str(exc) else ""
        print(SEP)
        print(f"Run stopped: {type(exc).__name__}{detail}")
        if rj.exists():
            print(f"\nCheckpoint saved ({rj}): run the scrape again with the same name and "
                  "output folder to continue it.\n")
        raise SystemExit(1)
    # the requested window, not the saved posts' span: `verify` must check the whole of it
    attrs = {"scrape_window": {"date_min": params.date_min.date().isoformat(),
                               "date_max": params.date_max.date().isoformat()}}
    if params.owner_id is not None:  # a rebuilt participants base keeps its Owner ID
        attrs["owner_id"] = params.owner_id
    path, rows, span = _write_posts(ckpt_dir, params.out_dir, params.name, attrs)
    print(f"Posts:    {path}  ({rows} rows)")

    r_path = None
    if params.with_reactors and _shard_paths(ckpt_dir, "reactors"):
        # streams the shards, deduplicating on REACTOR_DEDUP_KEY (see _consolidate_reactors)
        r_path, n_reactors = _consolidate_reactors(
            ckpt_dir, params.out_dir / f"{params.name}_reactors{span}.parquet")
        print(f"Reactors: {r_path}  ({n_reactors} rows)")

    if params.with_participants:
        p_out = params.out_dir / f"{params.name}_participants{span}"
        try:
            participants(str(path), str(p_out),
                         reactors=str(r_path) if r_path else "", fmt="parquet",
                         owner_id=params.owner_id)
        except SystemExit as exc:  # nothing to build (no comments, no reactors, ...)
            print(f"Participants: skipped ({exc})")

    _clear_checkpoint(ckpt_dir)  # clean finish -> drop the resume cursor and shards
    return path
