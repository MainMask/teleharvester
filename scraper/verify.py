"""Check a scraped posts file against the live channel.

`scrape` walks the channel with `iter_messages` (messages.getHistory). This probes
every id that is *absent* from the scrape with `get_messages(ids=...)`
(messages.getMessages) — an independent API path — so a gap that is a real,
un-scraped post can be told apart from an ordinary deletion.
"""

import asyncio
import itertools
from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd
from telethon import TelegramClient
from telethon.errors import ChannelPrivateError, FloodWaitError
from telethon.tl.types import MessageService

from scraper.analysis import _count_comments
from scraper.config import Credentials, session_for, start_kwargs
from scraper.datafiles import read_table, resolve_inputs, save_table
from scraper.scrape import (
    CLIENT_KWARGS,
    NET_ERRORS,
    RETRYABLE_RPC,
    SEP,
    _channel_ref,
    _warm_channel,
)

ID_BATCH = 200          # messages.getMessages accepts up to 200 ids per call
BATCH_PAUSE = 0.3       # gentle spacing between probe batches
BOUND_PROBE_CAP = 5000  # cap the id sweep beyond the scraped range


@dataclass
class VerifyParams:
    input: str
    channel: str
    date_min: datetime
    date_max: datetime
    session: str = "scraper"
    output: str = ""
    comment_sample: int = 0


def _chunks(iterable, n):
    """Yield successive lists of up to `n` items. Works on a list or a lazy
    generator, so a huge id range can be probed without materialising it."""
    it = iter(iterable)
    while batch := list(itertools.islice(it, n)):
        yield batch


def _load_saved(pattern: str, group: str) -> pd.DataFrame:
    frames = []
    for p in resolve_inputs(pattern):
        df = read_table(p)
        # a folder input also holds the run's _reactors / _participants files
        if "Reactor ID" in df.columns or "Message ID" not in df.columns:
            print(f"  - skipped {p.name}: not a posts file")
            continue
        frames.append(df)
    if not frames:
        raise SystemExit(f"{pattern}: no scraped posts file — pass the *_posts file, not *_reactors")
    df = pd.concat(frames, ignore_index=True)
    if "Group" in df.columns:  # a multi-channel scrape: ids of other channels are not ours
        groups = df["Group"].astype(str)
        df = df[groups.str.lower() == group.lower()]
        if df.empty:
            raise SystemExit(f"{pattern}: no rows for {group}; groups in the file: "
                             f"{sorted(set(groups.dropna()))}")
    df = df[df["Message ID"].notna()].copy()
    # dedup on the int: _posts files store the id as str, _until_ snapshots as int
    df["_id"] = df["Message ID"].astype(int)
    return df.drop_duplicates(subset="_id")


async def _classify_absent(client, entity, ids, params: VerifyParams):
    """Probe `ids`; return ([(id, date), ...] real in-window posts, counts dict)."""
    missed, counts = [], {"deleted": 0, "service": 0, "out_of_window": 0}
    for batch in _chunks(ids, ID_BATCH):
        for mid, m in zip(batch, await client.get_messages(entity, ids=batch)):
            if m is None:
                counts["deleted"] += 1
            elif isinstance(m, MessageService) or getattr(m, "action", None) is not None:
                counts["service"] += 1
            elif not (params.date_min <= m.date <= params.date_max):
                counts["out_of_window"] += 1
            else:
                missed.append((mid, m.date))
        await asyncio.sleep(BATCH_PAUSE)
    return missed, counts


async def _check_comments(client, entity, df: pd.DataFrame, params: VerifyParams):
    """Sample scraped threads, compare captured comment counts to the server's."""
    if "Comments" in df.columns:
        nc = df.set_index("_id")["Comments"]
        if "Comments List" in df.columns:  # rows from an _until_ snapshot carry no count
            nc = nc.fillna(df.set_index("_id")["Comments List"].apply(_count_comments))
        nc = nc.astype(int)
    elif "Comments List" in df.columns:
        nc = df.set_index("_id")["Comments List"].apply(_count_comments)
    else:
        print("comment check: input has no 'Comments'/'Comments List' column — skipped")
        return []
    # every post with nothing captured (a whole lost thread), plus a sample of the rest
    zero = list(nc[nc == 0].index)
    with_c = nc[nc > 0]
    n = min(params.comment_sample, len(with_c))
    ids = sorted(zero + list(with_c.sample(n, random_state=0).index))
    if not ids:
        return []
    print(f"comment threads: re-checking {n} of {len(with_c)} threads and all {len(zero)} "
          f"post(s) with no captured comments against the server...")
    short = []
    for batch in _chunks(ids, ID_BATCH):
        for m in await client.get_messages(entity, ids=batch):
            # only a channel post's comment section; a group's reply thread is never scraped
            if m is None or not getattr(m, "replies", None) or not m.replies.comments:
                continue
            expected = m.replies.replies or 0
            got = int(nc.get(m.id, 0))
            if got < expected * 0.9 and expected - got > 5:
                short.append((m.id, m.date, got, expected))
        await asyncio.sleep(BATCH_PAUSE)
    for mid, mdate, got, exp in short:
        print(f"    ~ short thread id {mid}  captured {got} / server {exp}")
    return short


async def _verify(creds: Credentials, params: VerifyParams):
    ref = _channel_ref(params.channel)
    df = _load_saved(params.input, f"@{ref.slug}")
    saved = set(df["_id"])
    id_min, id_max = min(saved), max(saved)

    client = TelegramClient(session_for(creds, params.session), creds.api_id, creds.api_hash,
                            **CLIENT_KWARGS)
    await client.start(**start_kwargs(creds))

    flagged = []          # (id, date, reason)
    short_threads = []
    try:
        await _warm_channel(client, ref, dialogs_loaded=False)
        try:
            entity = await client.get_entity(ref.arg)
        except (ValueError, ChannelPrivateError) as exc:  # unknown username, or a chat this account is not in
            raise SystemExit(f"{params.channel}: {exc}")
        newest = await client.get_messages(entity, limit=1)
        oldest = await client.get_messages(entity, limit=1, reverse=True)
        total = (await client.get_messages(entity, limit=0)).total
        newest = newest[0] if newest else None
        oldest = oldest[0] if oldest else None

        print(SEP)
        print(f'verify "{params.input}"  vs  {params.channel} '
              f'("{getattr(entity, "title", "")}")')
        print(f"  saved posts:   {len(saved):>8}   (id {id_min}..{id_max})")
        print(f"  channel total: {total:>8}   "
              f"(newest id {getattr(newest, 'id', '?')}, oldest id {getattr(oldest, 'id', '?')})")
        print(SEP)

        # check: every id absent from the scrape, within the scraped range. Stream the
        # ids (id_min..id_max can span millions): the count is exact arithmetically
        # (id_min/id_max are min/max of the saved set, so every saved id is in range).
        n_absent = (id_max - id_min + 1) - len(saved)
        absent = (i for i in range(id_min, id_max + 1) if i not in saved)
        note = "  (this will take a few minutes)" if n_absent > 20_000 else ""
        print(f"id range {id_min}..{id_max}: {n_absent} absent id(s), probing...{note}")
        missed, counts = await _classify_absent(client, entity, absent, params)
        print(f"  {counts['deleted']} deleted/never existed, {counts['service']} service, "
              f"{counts['out_of_window']} outside dates, {len(missed)} REAL POSTS MISSED")
        for mid, mdate in missed:
            print(f"    ! missed id {mid}  {mdate:%Y-%m-%d %H:%M}")
            flagged.append((mid, mdate, "missed"))

        # check: the scrape reached the oldest in-window message. The window starts
        # right after the last message older than date_min (or at the channel's first).
        before = await client.get_messages(entity, limit=1, offset_date=params.date_min)
        lo_start = before[0].id + 1 if before else getattr(oldest, "id", id_min)
        if lo_start < id_min:
            capped = id_min - lo_start > BOUND_PROBE_CAP
            lo = range(lo_start, min(id_min, lo_start + BOUND_PROBE_CAP))
            lo_missed, _ = await _classify_absent(client, entity, list(lo), params)
            print(f"lower bound: window starts at id {lo_start} < first saved {id_min} -> "
                  f"{'>=' if capped else ''}{len(lo_missed)} in-window post(s) before the scrape")
            for mid, mdate in lo_missed:
                flagged.append((mid, mdate, "before-first-saved"))

        # check: nothing in-window newer than the last saved id (offset_date is exclusive)
        last_in = await client.get_messages(entity, limit=1,
                                            offset_date=params.date_max + timedelta(seconds=1))
        if last_in and last_in[0].id > id_max:
            capped = last_in[0].id - id_max > BOUND_PROBE_CAP
            hi = range(id_max + 1, min(last_in[0].id + 1, id_max + 1 + BOUND_PROBE_CAP))
            hi_missed, _ = await _classify_absent(client, entity, list(hi), params)
            print(f"upper bound: {'>=' if capped else ''}{len(hi_missed)} in-window post(s) "
                  f"after the last saved id {id_max}")
            for mid, mdate in hi_missed:
                flagged.append((mid, mdate, "after-last-saved"))

        if params.comment_sample:
            short_threads = await _check_comments(client, entity, df, params)
    finally:
        await client.disconnect()

    return flagged, short_threads


def run(creds: Credentials, params: VerifyParams) -> None:
    try:
        flagged, short_threads = asyncio.run(_verify(creds, params))
    except (KeyboardInterrupt, *NET_ERRORS, FloodWaitError, *RETRYABLE_RPC) as exc:
        print(SEP)
        print(f"Verification interrupted ({type(exc).__name__}) — re-run to finish.")
        raise SystemExit(1)
    print(SEP)

    if params.output and (flagged or short_threads):
        rows = [{"Message ID": i, "Date": d, "Reason": r} for i, d, r in flagged]
        rows += [{"Message ID": i, "Date": d, "Reason": "short-thread"}
                 for i, d, _, _ in short_threads]
        out = save_table(pd.DataFrame(rows), params.output, "parquet")
        print(f"flagged ids -> {out}")

    if flagged:
        print(f"RESULT: {len(flagged)} message(s) missed by the scrape — re-scrape "
              f"the affected date range(s).")
        raise SystemExit(1)
    if short_threads:
        print(f"RESULT: 0 posts missed; {len(short_threads)} thread(s) look short "
              f"(server counts include deleted comments — usually benign).")
        return
    print("RESULT: 0 posts missed — the scrape is complete for this id range.")
