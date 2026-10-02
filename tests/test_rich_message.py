"""Offline tests for rich broadcast content: aiogram -> Telethon entity mapping."""
from types import SimpleNamespace

from telethon import types

from modules.rich_message import (
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
