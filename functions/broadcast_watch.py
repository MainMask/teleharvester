"""🆕 Комментарии — к новым постам: the workers watch channels and comment each new post one by one, a pause
apart, until ⏹."""
import asyncio
import contextlib
import os
from datetime import date, datetime, timedelta, timezone

from telethon import utils
from telethon.tl.types import MessageService

from functions.base import TelethonFunction
from functions.base.base import AccountLimited, pick_seconds
from functions.broadcast import LISTENER_ENTITY_LIMIT
from functions.broadcast_comments import comments_cap, variants, worker_key
from modules import rich_message
from modules.storages.sessions_storage import connect_client
from scraper.scrape import _channel_ref

POLL_SECONDS = 60  # how often the channels are read for new posts: no worker has to be in them for updates
READ_LIMIT = 20    # a channel's newest posts read a round
FRESH_FOR = timedelta(hours=3)  # an older post found (missed while the bot was down) is not commented


def _target(channel: str):
    """What Telethon reads a typed channel (CHAT_FORMAT) by, as the scraper does (_channel_ref): an id or a t.me/c/
    link as its peer (a string id would be taken for a phone), a t.me/s/ or a post's link as @name."""
    arg = _channel_ref(channel).arg
    if isinstance(arg, int):
        bare, kind = utils.resolve_id(arg)
        return kind(bare)
    return arg


class CommentsWatchFunc(TelethonFunction):
    """Comment the new posts of channels"""

    async def run(self, channels, per_post, gap, contents, report, last=None, on_seen=None):
        """`per_post`: comments under each new post, each by another worker (0: by every one); `gap`: [sec] or
        [min, max], the pause before each of them (the first one after the post is seen); `contents`: the message,
        or the variants a comment takes one of; `last`: the newest post seen of each channel, as a run before a
        restart left it (its channels' newer posts are new to this one); `on_seen(last)`: told each time it grows."""
        self.progress_total(None)  # how many posts will come is unknown
        self._workers, self._out = list(self.sessions), set()
        self._turns = {"read": 0, "post": 0}  # apart: the reads must not decide who comments first
        self._unread = set()
        self._albums = {}  # channel -> the albums of its last read (see _threads)
        self._peers = {}  # (id(session), channel) -> its peer, looked up once (see _peer)
        self._limits, self._capped = comments_cap(self.settings), {}  # capped: id(worker) -> the day it was said
        per_post = min(per_post or len(self._workers), len(self._workers))
        posts = set()  # the comments' tasks, a post each
        async with contextlib.AsyncExitStack() as stack:
            for session in self._workers:  # connected for the whole job; a dropped one reconnects at its next use
                await stack.enter_async_context(self.storage.ainitialize_session(session))
            seen = {}  # channel -> the newest post seen
            for channel in channels:
                if last and channel in last:
                    seen[channel] = last[channel]
                elif (found := await self._read(channel, report, limit=1)) is not None:
                    seen[channel] = found[0].id if found else 0
            if not seen:
                await report("Ни один канал не читается — слежка остановлена.")
                return
            if on_seen:
                on_seen(dict(seen))
            await report(f"Слежу за каналами: {len(seen)}. Новые посты проверяются раз в {POLL_SECONDS} с; "
                         f"под каждым — комментариев: {per_post}.")
            try:
                while self._live():
                    await asyncio.sleep(POLL_SECONDS)
                    self._limits = comments_cap(self.settings)  # a cap changed in ⚙️ applies from this round
                    for session in self._workers:  # connected all along, each gets its chats' updates: Telethon
                        if len(session.session._entities) > LISTENER_ENTITY_LIMIT:  # keeps them all, as for 🔔
                            session.session._entities.clear()
                    for channel in seen:
                        if not self._live():  # all out by a limit: none to read with
                            break
                        found = await self._read(channel, report, min_id=seen[channel], limit=READ_LIMIT)
                        if not found:
                            continue
                        seen[channel] = max(post.id for post in found)
                        if on_seen:
                            on_seen(dict(seen))
                        old = 0
                        for post in self._threads(channel, found):
                            where = f"{channel} · пост {post.id}"
                            if post.date < datetime.now(timezone.utc) - FRESH_FOR:
                                old += 1
                            elif post.replies is None or not post.replies.comments:
                                await report(f"{where}: комментарии закрыты — пропущен")
                            else:
                                task = asyncio.create_task(self._comment(channel, post.id, where, per_post, gap,
                                                                         variants(contents), report))
                                posts.add(task)
                                task.add_done_callback(posts.discard)
                        if old:
                            await report(f"{channel}: пропущено старых постов (старше "
                                         f"{FRESH_FOR.seconds // 3600} ч): {old}")
                await report("Все воркеры выбыли по лимитам — слежка остановлена.")
            finally:  # ⏹ or the end: the comments not written yet are not written
                for task in posts:
                    task.cancel()
                await asyncio.gather(*posts, return_exceptions=True)

    def _live(self) -> bool:
        return any(id(session) not in self._out for session in self._workers)

    def _order(self, kind: str) -> list:
        """The workers by turn, the ones out by a limit left out: each read ("read") and each post ("post") starts
        with the next one of its own turn."""
        start = self._turns[kind]
        self._turns[kind] += 1
        turn = self._workers[start % len(self._workers):] + self._workers[:start % len(self._workers)]
        return [session for session in turn if id(session) not in self._out]

    def _name(self, session) -> str:
        return os.path.basename(self.storage.get_session_path(session) or "") or "worker"

    async def _read(self, channel, report, **kwargs):
        """The channel's posts (get_messages' `kwargs`) as the first worker that can read it has them; None if none
        can. Said once when it stops being read, and once when it is read again."""
        error = None
        for session in self._order("read"):
            try:
                await connect_client(session)  # a no-op if connected
                found = await session.get_messages(await self._peer(session, channel), **kwargs)
            except Exception as err:  # its access hash is this worker's own: the next one resolves it anew
                error = err
                continue
            if channel in self._unread:
                self._unread.discard(channel)
                await report(f"{channel}: снова читается")
            return found
        if channel not in self._unread:
            self._unread.add(channel)
            await report(f"[!] {channel} не читается: {error}")
        return None

    async def _peer(self, session, channel):
        """The channel's peer for this worker, looked up once: an invite's link is checked anew at every use
        (CheckChatInvite), once a round it would end in a flood wait."""
        key = (id(session), channel)
        if key not in self._peers:
            self._peers[key] = await session.get_input_entity(await self.resolve_chat(session, _target(channel)))
        return self._peers[key]

    def _threads(self, channel, found) -> list:
        """The posts to comment, oldest first: an album once — its post with the comments —, even split by a round
        (only the channel's last read can hold its start: no older album is kept); a pin or a new photo (a service
        message) is no post."""
        taken = self._albums.get(channel, set())
        chosen = {}
        for post in sorted(found, key=lambda post: post.id):
            if isinstance(post, MessageService) or post.grouped_id in taken:  # an album's rest: taken last round
                continue
            key = ("album", post.grouped_id) if post.grouped_id is not None else ("post", post.id)
            if key not in chosen or (post.replies and not chosen[key].replies):
                chosen[key] = post
        self._albums[channel] = {post.grouped_id for post in found if post.grouped_id is not None}
        return sorted(chosen.values(), key=lambda post: post.id)

    async def _capped_today(self, session, report) -> bool:
        """The worker wrote its daily comments ([limits] comments_per_account_daily): said once a day."""
        if not self._limits.reached(worker_key(self.storage, session, self._limits)):
            return False
        if self._capped.get(id(session)) != (today := date.today()):
            self._capped[id(session)] = today
            await report(f"[{self._name(session)}] дневной лимит комментариев — до завтра")
        return True

    async def _comment(self, channel, post_id, where, per_post, gap, texts, report):
        """`per_post` comments under the post, each by another worker, each after a pause of `gap`; `texts`: the
        contents to send, one a comment (variants)."""
        done = 0
        for session in self._order("post"):
            if done >= per_post:
                return
            if await self._capped_today(session, report):
                continue
            await asyncio.sleep(pick_seconds(gap))
            if id(session) in self._out or await self._capped_today(session, report):  # meanwhile, under another post
                continue
            name = self._name(session)
            try:
                await connect_client(session)
                peer = await self._peer(session, channel)
                await rich_message.send(session, peer, next(texts), self.safe_call, report=report,
                                        comment_to=post_id)
            except AccountLimited as err:
                self._out.add(id(session))
                self.progress_failed()
                await report(f"[{name}] лимит — больше не комментирует. {err}")
            except Exception as err:
                self.progress_failed()
                await report(f"[{name}] {where}: не отправлено — {err}")
            else:
                self._limits.bump(worker_key(self.storage, session, self._limits))
                done += 1
                self.progress_step()
                self.progress_ok()
                await report(f"[{name}] {where}: комментарий {done}/{per_post}")
        if not done:
            await report(f"{where}: ни одного комментария — воркеры на лимитах или с ошибками (выше)")
