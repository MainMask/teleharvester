"""Offline tests for broadcast message capture and temp-file lifecycle."""
import asyncio
import glob
import os
import tempfile
from types import SimpleNamespace

import pytest

from bot.routers._common import build_content
from bot.services.capture import capture
from modules.rich_message import RichContent


def msg(**kw):
    """A fake aiogram message with every media slot empty unless overridden."""
    base = dict(
        text=None, caption=None, caption_entities=None, entities=None,
        photo=None, video=None, animation=None, voice=None,
        video_note=None, audio=None, sticker=None, document=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class _Bot:
    def __init__(self, fail=False):
        self.fail = fail

    async def download(self, source, destination=None):
        if self.fail:
            raise RuntimeError("file is too big")  # mimics the Bot API ~20 MB cap
        with open(destination, "w") as fileobj:
            fileobj.write("x")


class TestCleanup:
    def test_cleanup_removes_temp_dir(self):
        directory = tempfile.mkdtemp(prefix="bcast_test_")
        path = os.path.join(directory, "f.bin")
        with open(path, "w") as fileobj:
            fileobj.write("x")

        content = RichContent(text="", temp_paths=[path], temp_dir=directory)
        content.cleanup()

        assert not os.path.exists(path)
        assert not os.path.exists(directory)  # the dir itself is gone, not just its files


class TestCapture:
    def test_text_only_creates_no_temp_dir(self):
        content = asyncio.run(capture(msg(text="hi"), _Bot(), None))
        assert content.text == "hi"
        assert content.media == []
        assert content.temp_dir is None  # lazy: nothing on disk for text-only sends

    def test_photo_downloaded(self):
        message = msg(caption="pic", photo=[SimpleNamespace()])
        content = asyncio.run(capture(message, _Bot(), None))
        assert content.text == "pic"
        assert len(content.media) == 1
        assert os.path.exists(content.media[0].path)
        content.cleanup()

    def test_failed_download_cleans_up(self):
        before = set(glob.glob(os.path.join(tempfile.gettempdir(), "bcast_*")))

        message = msg(caption="pic", photo=[SimpleNamespace()])
        with pytest.raises(RuntimeError):
            asyncio.run(capture(message, _Bot(fail=True), None))

        after = set(glob.glob(os.path.join(tempfile.gettempdir(), "bcast_*")))
        assert after == before  # no leftover temp dir on failure


class TestBuildContent:
    def _message(self, bot, **kw):
        replies = []
        message = msg(bot=bot, **kw)
        message.answer = lambda text: replies.append(text) or _noop()
        message._replies = replies
        return message

    def test_rejects_empty_message(self):
        # e.g. a poll/contact/location: no text and no recognizable media.
        before = set(glob.glob(os.path.join(tempfile.gettempdir(), "bcast_*")))
        message = self._message(_Bot())

        result = asyncio.run(build_content(message, None))

        assert result is None
        assert any("нельзя разослать" in r for r in message._replies)
        assert set(glob.glob(os.path.join(tempfile.gettempdir(), "bcast_*"))) == before

    def test_reports_download_failure(self):
        message = self._message(_Bot(fail=True), caption="pic", photo=[SimpleNamespace()])

        result = asyncio.run(build_content(message, None))

        assert result is None
        assert any("Не удалось" in r for r in message._replies)

    def test_passes_through_valid_text(self):
        message = self._message(_Bot(), text="hi")

        result = asyncio.run(build_content(message, None))

        assert result is not None and result.text == "hi"
        assert message._replies == []


async def _noop():
    return None
