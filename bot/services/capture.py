"""Capture an operator's Telegram message (text/media/album) into a RichContent.

Media is downloaded via the Bot API (note: Bot API caps downloads at ~20 MB) so the
worker accounts can re-upload it; formatting entities and custom emoji are carried
through modules.rich_message.
"""
import os
import shutil
import tempfile

from modules.rich_message import MediaItem, RichContent, convert_entities

# Broadcast media is downloaded here (project-local, not system /tmp) so a process
# killed mid-send — e.g. the OOM killer, which never runs content.cleanup() — leaves
# its temp dirs somewhere clear_orphan_temp() can sweep on the next start.
BROADCAST_TMP_DIR = os.path.join("tmp", "broadcast")


def clear_orphan_temp() -> None:
    """Drop broadcast temp dirs left by a previous process. Safe at startup: no
    broadcast is in flight yet, so nothing live is removed."""
    shutil.rmtree(BROADCAST_TMP_DIR, ignore_errors=True)


def _source_and_flags(message):
    """Return (downloadable, filename, force_document, voice, video_note) or None."""
    if message.photo:
        return message.photo[-1], "photo.jpg", False, False, False
    if message.video:
        return message.video, message.video.file_name or "video.mp4", False, False, False
    if message.animation:
        return message.animation, message.animation.file_name or "animation.mp4", False, False, False
    if message.voice:
        return message.voice, "voice.ogg", False, True, False
    if message.video_note:
        return message.video_note, "video_note.mp4", False, False, True
    if message.audio:
        return message.audio, message.audio.file_name or "audio.mp3", False, False, False
    if message.sticker:
        ext = ".tgs" if message.sticker.is_animated else ".webp"
        return message.sticker, f"sticker{ext}", False, False, False
    if message.document:
        return message.document, message.document.file_name or "document", True, False, False
    return None


async def _download(bot, directory, index, found) -> MediaItem:
    source, filename, force_document, voice, video_note = found
    path = os.path.join(directory, f"{index}_{filename}")
    await bot.download(source, destination=path)

    return MediaItem(
        path=path,
        force_document=force_document,
        voice_note=voice,
        video_note=video_note,
    )


async def capture(message, bot, album=None) -> RichContent:
    messages = album or [message]

    text = ""
    entities_src = None
    for item in messages:
        if item.caption or item.text:
            text = item.caption or item.text
            entities_src = item.caption_entities or item.entities
            break

    content = RichContent(text=text, entities=convert_entities(entities_src))

    # Create the temp dir lazily (only if there is media) so text-only sends leave
    # nothing behind; clean up partial downloads if one fails (e.g. the Bot API's ~20 MB cap).
    try:
        for index, item in enumerate(messages):
            found = _source_and_flags(item)
            if not found:
                continue

            if content.temp_dir is None:
                os.makedirs(BROADCAST_TMP_DIR, exist_ok=True)
                content.temp_dir = tempfile.mkdtemp(prefix="bcast_", dir=BROADCAST_TMP_DIR)

            media = await _download(bot, content.temp_dir, index, found)
            content.media.append(media)
            content.temp_paths.append(media.path)
    except Exception:
        content.cleanup()
        raise

    return content
