"""Rich broadcast content: text + full formatting (incl. custom emoji) + any media.

Captured once from the operator's message (see bot/services/capture.py) and replayed
by every worker via Telethon's `formatting_entities`. Telethon and the Bot API both use
UTF-16 entity offsets, so aiogram entities map 1:1 with no recalculation.
"""
import os
import shutil
from dataclasses import dataclass, field
from typing import List, Optional

from telethon import types

from functions.base.base import AccountLimited


# aiogram entity type -> builder(ent) returning a Telethon MessageEntity.
# Auto-detected types (mention, url, email, phone_number, hashtag, cashtag,
# bot_command, date_time) are intentionally omitted: Telegram re-detects them.
_BUILDERS = {
    "bold": lambda e: types.MessageEntityBold(e.offset, e.length),
    "italic": lambda e: types.MessageEntityItalic(e.offset, e.length),
    "underline": lambda e: types.MessageEntityUnderline(e.offset, e.length),
    "strikethrough": lambda e: types.MessageEntityStrike(e.offset, e.length),
    "spoiler": lambda e: types.MessageEntitySpoiler(e.offset, e.length),
    "code": lambda e: types.MessageEntityCode(e.offset, e.length),
    "pre": lambda e: types.MessageEntityPre(e.offset, e.length, language=e.language or ""),
    "blockquote": lambda e: types.MessageEntityBlockquote(e.offset, e.length, collapsed=False),
    "expandable_blockquote": lambda e: types.MessageEntityBlockquote(e.offset, e.length, collapsed=True),
    "text_link": lambda e: types.MessageEntityTextUrl(e.offset, e.length, url=e.url),
    "text_mention": lambda e: types.MessageEntityMentionName(e.offset, e.length, user_id=e.user.id),
    "custom_emoji": lambda e: types.MessageEntityCustomEmoji(e.offset, e.length, document_id=int(e.custom_emoji_id)),
}


def convert_entities(aiogram_entities) -> list:
    """Map aiogram message entities to Telethon formatting entities (offsets copied verbatim)."""
    result = []
    for entity in aiogram_entities or []:
        builder = _BUILDERS.get(str(entity.type))
        if builder:
            result.append(builder(entity))
    return result


def has_custom_emoji(entities) -> bool:
    return any(isinstance(e, types.MessageEntityCustomEmoji) for e in entities)


def strip_custom_emoji(entities) -> list:
    """Drop only custom-emoji entities (other formatting kept) for non-Premium fallback."""
    return [e for e in entities if not isinstance(e, types.MessageEntityCustomEmoji)]


@dataclass
class MediaItem:
    path: str
    force_document: bool = False
    voice_note: bool = False
    video_note: bool = False
    animated: bool = False  # a GIF: an .mp4 without this attribute arrives as a plain video


@dataclass
class RichContent:
    text: str = ""
    entities: List = field(default_factory=list)
    media: List[MediaItem] = field(default_factory=list)
    temp_paths: List[str] = field(default_factory=list)
    temp_dir: Optional[str] = None

    def cleanup(self):
        for path in self.temp_paths:
            try:
                os.remove(path)
            except OSError:
                pass
        self.temp_paths = []

        if self.temp_dir:
            shutil.rmtree(self.temp_dir, ignore_errors=True)
            self.temp_dir = None


async def _send_once(session, peer, content, entities, safe_call, **kwargs):
    if not content.media:
        await safe_call(lambda: session.send_message(
            peer, content.text, formatting_entities=entities, **kwargs
        ))
    elif len(content.media) == 1:
        item = content.media[0]
        await safe_call(lambda: session.send_file(
            peer, item.path,
            caption=content.text,
            formatting_entities=entities,
            parse_mode=None,  # with no entities send_file would parse the caption as markdown
            force_document=item.force_document,
            voice_note=item.voice_note,
            video_note=item.video_note,
            attributes=[types.DocumentAttributeAnimated()] if item.animated else None,
            **kwargs,
        ))
    else:
        paths = [item.path for item in content.media]
        all_docs = all(item.force_document for item in content.media)
        await safe_call(lambda: session.send_file(
            peer, paths,
            caption=content.text,
            formatting_entities=entities,
            parse_mode=None,  # with no entities send_file would parse the caption as markdown
            force_document=all_docs,
            **kwargs,
        ))


async def _resolve_mentions(session, entities) -> list:
    """MessageEntityMentionName -> InputMessageEntityMentionName.

    Telethon converts mentions only while parsing text itself; with formatting_entities
    the output-only type would reach the server as-is. An unresolvable user's mention is
    dropped (its text stays), as Telethon's own _replace_with_mention does.
    """
    result = []
    for entity in entities:
        if isinstance(entity, types.MessageEntityMentionName):
            try:
                user = await session.get_input_entity(entity.user_id)
            except (ValueError, TypeError):
                continue
            entity = types.InputMessageEntityMentionName(entity.offset, entity.length, user)
        result.append(entity)
    return result


async def send(session, peer, content, safe_call, report=None, **kwargs):
    """Send `content` to `peer`, auto-stripping custom emoji if the worker can't send them.

    All sends go through `safe_call` (rate-limit handling). On a send failure where the
    content carries custom emoji (sending those needs Telegram Premium), retry once with
    them stripped so the rest of the formatting still goes out.
    """
    entities = await _resolve_mentions(session, content.entities)
    try:
        await _send_once(session, peer, content, entities, safe_call, **kwargs)
    except AccountLimited:
        raise
    except Exception:
        if not has_custom_emoji(entities):
            raise
        if report:
            await report("custom emoji not sent (needs Premium), retrying without them")
        await _send_once(session, peer, content, strip_custom_emoji(entities), safe_call, **kwargs)
