"""Offline tests for rich broadcast content: aiogram -> Telethon entity mapping."""
import asyncio
from types import SimpleNamespace

from telethon import errors, types

from modules.rich_message import (
    MediaItem,
    RichContent,
    _send_once,
    send,
    convert_entities,
    has_custom_emoji,
    strip_custom_emoji,
)


def ent(type_, offset, length, **extra):
    return SimpleNamespace(type=type_, offset=offset, length=length, **extra)


class TestConvertEntities:
    def test_basic_styles(self):
        out = convert_entities([
            ent("bold", 0, 4),
            ent("italic", 4, 2),
            ent("underline", 0, 1),
            ent("strikethrough", 1, 1),
            ent("spoiler", 2, 3),
            ent("code", 0, 5),
        ])
        assert [type(e).__name__ for e in out] == [
            "MessageEntityBold", "MessageEntityItalic", "MessageEntityUnderline",
            "MessageEntityStrike", "MessageEntitySpoiler", "MessageEntityCode",
        ]
        assert (out[0].offset, out[0].length) == (0, 4)

    def test_custom_emoji(self):
        out = convert_entities([ent("custom_emoji", 3, 2, custom_emoji_id="5377498341074542641")])
        assert isinstance(out[0], types.MessageEntityCustomEmoji)
        assert out[0].document_id == 5377498341074542641
        assert (out[0].offset, out[0].length) == (3, 2)

    def test_blockquotes(self):
        out = convert_entities([
            ent("blockquote", 0, 3),
            ent("expandable_blockquote", 3, 3),
        ])
        assert out[0].collapsed is False
        assert out[1].collapsed is True

    def test_text_link_pre_mention(self):
        out = convert_entities([
            ent("text_link", 0, 4, url="https://x.y"),
            ent("pre", 4, 5, language="python"),
            ent("text_mention", 9, 3, user=SimpleNamespace(id=777)),
        ])
        assert isinstance(out[0], types.MessageEntityTextUrl) and out[0].url == "https://x.y"
        assert isinstance(out[1], types.MessageEntityPre) and out[1].language == "python"
        assert isinstance(out[2], types.MessageEntityMentionName) and out[2].user_id == 777

    def test_auto_detected_skipped(self):
        # Telegram re-detects these; we don't carry them.
        out = convert_entities([
            ent("url", 0, 5),
            ent("mention", 5, 4),
            ent("email", 9, 7),
            ent("phone_number", 0, 11),
            ent("hashtag", 0, 3),
            ent("bot_command", 0, 4),
        ])
        assert out == []

    def test_offsets_copied_verbatim(self):
        # UTF-16 crux: "😀x" with bold over the whole string is length 3 in Bot API;
        # convert copies offsets as-is (Telethon formatting_entities uses UTF-16 too).
        out = convert_entities([ent("bold", 0, 3)])
        assert (out[0].offset, out[0].length) == (0, 3)


class TestCustomEmojiHelpers:
    def _mixed(self):
        return convert_entities([
            ent("bold", 0, 4),
            ent("custom_emoji", 4, 2, custom_emoji_id="123"),
            ent("italic", 6, 2),
        ])

    def test_has_custom_emoji(self):
        assert has_custom_emoji(self._mixed()) is True
        assert has_custom_emoji(convert_entities([ent("bold", 0, 1)])) is False

    def test_strip_removes_only_custom_emoji(self):
        stripped = strip_custom_emoji(self._mixed())
        assert [type(e).__name__ for e in stripped] == ["MessageEntityBold", "MessageEntityItalic"]


class _Worker:
    """send_file stub: records what it was given, returns messages carrying fresh media."""

    def __init__(self, expire_cached=False):
        self.files = []
        self.expire_cached = expire_cached

    async def send_file(self, peer, file, **kw):
        self.files.append(file)
        if self.expire_cached and "media:" in str(file):  # a re-send of an uploaded file
            self.expire_cached = False
            raise errors.FileReferenceExpiredError(request=None)
        if isinstance(file, list):
            return [SimpleNamespace(media=f"media:{f}") for f in file]
        return SimpleNamespace(media=f"media:{file}")


async def _direct(make):
    return await make()


def _send(worker, content):
    asyncio.run(_send_once(worker, "peer", content, [], _direct))


class TestMediaUploadedOnce:
    def test_second_send_reuses_the_workers_media(self):
        content = RichContent(media=[MediaItem(path="a.jpg")])
        worker = _Worker()
        _send(worker, content)
        _send(worker, content)
        assert worker.files == ["a.jpg", "media:a.jpg"]

    def test_each_worker_uploads_its_own(self):
        content = RichContent(media=[MediaItem(path="a.jpg")])
        first, second = _Worker(), _Worker()
        _send(first, content)
        _send(second, content)
        assert first.files == ["a.jpg"] and second.files == ["a.jpg"]

    def test_album_reuses_the_media_list(self):
        content = RichContent(media=[MediaItem(path="a.jpg"), MediaItem(path="b.jpg")])
        worker = _Worker()
        _send(worker, content)
        _send(worker, content)
        assert worker.files == [["a.jpg", "b.jpg"], ["media:a.jpg", "media:b.jpg"]]

    def test_expired_reference_uploads_again(self):
        content = RichContent(media=[MediaItem(path="a.jpg")])
        worker = _Worker()
        _send(worker, content)
        worker.expire_cached = True
        _send(worker, content)  # the cached media is refused: the file goes again
        _send(worker, content)  # ...and the fresh upload is cached
        assert worker.files == ["a.jpg", "media:a.jpg", "a.jpg", "media:a.jpg"]

    def test_send_without_a_message_is_not_cached(self):
        class _NoMessage:
            files = []

            async def send_file(self, peer, file, **kw):
                self.files.append(file)

        content = RichContent(media=[MediaItem(path="a.jpg")])
        worker = _NoMessage()
        _send(worker, content)
        _send(worker, content)
        assert worker.files == ["a.jpg", "a.jpg"]


class _Refusing:
    """send_message stub that raises `error` on every call."""

    def __init__(self, error):
        self.error, self.calls = error, 0

    async def send_message(self, peer, text, **kw):
        self.calls += 1
        raise self.error


def _send_rich(worker, reports):
    async def report(text):
        reports.append(text)

    content = RichContent(text="hi 🙂", entities=convert_entities([ent("custom_emoji", 3, 2, custom_emoji_id="1")]))
    asyncio.run(send(worker, "peer", content, _direct, report=report))


class TestCustomEmojiRetry:
    def test_a_refusing_recipient_is_not_asked_twice(self):
        worker, reports = _Refusing(errors.PeerIdInvalidError(request=None)), []
        try:
            _send_rich(worker, reports)
        except errors.PeerIdInvalidError:
            pass
        assert worker.calls == 1 and reports == []

    def test_another_error_retries_without_the_custom_emoji(self):
        worker, reports = _Refusing(ValueError("premium")), []
        try:
            _send_rich(worker, reports)
        except ValueError:
            pass
        assert worker.calls == 2 and "Premium" in reports[0]
