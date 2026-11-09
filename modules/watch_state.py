"""🆕 Комментарии — к новым постам of a project kept on disk while they run, so a bot restart continues them:
what they were started with — the chat, the channels, the comments a post, the pause, the message or its variants —
and the newest post seen of each channel. In the project's folder: watch.json, the message's media in
watch_media/ (tmp/broadcast is swept at every start)."""
import base64
import os
import shutil
from dataclasses import asdict

from telethon.extensions import BinaryReader

from modules import json_file
from modules.rich_message import MediaItem, RichContent

STATE_NAME = "watch.json"
MEDIA_DIR = "watch_media"

# the bot is stopping: a watch cancelled now keeps its state, to be continued at the next start
shutting_down = False


def _path(home: str) -> str:
    return os.path.join(home, STATE_NAME)


def _media_dir(home: str) -> str:
    return os.path.join(home, MEDIA_DIR)


def _keep_media(home: str, content: RichContent):
    """The content's media moved into the project's watch_media/: it is the watch's now, not a temp file."""
    if not content.media:
        return
    into = _media_dir(home)
    os.makedirs(into, exist_ok=True)
    for media in content.media:
        target = os.path.join(into, os.path.basename(media.path))
        shutil.move(media.path, target)
        media.path = target
    content.cleanup()  # its emptied temp dir
    content.temp_paths, content.temp_dir = [], None


def save(home: str, chat_id: int, channels: list, per_post: int, gap: list, contents: list):
    """A watch about to start, its message's media moved into the project's folder."""
    for content in contents:
        _keep_media(home, content)
    json_file.save(_path(home), {
        "chat_id": chat_id, "channels": channels, "per_post": per_post, "gap": gap, "last": {},
        "contents": [{"text": content.text,
                      "entities": [base64.b64encode(bytes(entity)).decode() for entity in content.entities],
                      "media": [asdict(media) for media in content.media]} for content in contents],
    })


def update_last(home: str, last: dict):
    """The newest post seen of each channel: a restart continues after them."""
    try:  # the watch goes on whatever becomes of its file: at worst a restart starts it afresh
        state = json_file.load(_path(home), None)
        if state is not None:  # dropped meanwhile (⏹): nothing to keep
            json_file.save(_path(home), {**state, "last": last})
    except (OSError, ValueError):
        pass


def load(home: str) -> dict | None:
    """The project's watch to continue, its contents as RichContent again; None: none (or a broken file)."""
    try:
        state = json_file.load(_path(home), None)
        if state is None:
            return None
        state["contents"] = [RichContent(
            text=content["text"],
            entities=[BinaryReader(base64.b64decode(raw)).tgread_object() for raw in content["entities"]],
            media=[MediaItem(**media) for media in content["media"]]) for content in state["contents"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return state


def drop(home: str):
    """The watch is over (⏹, its end, or it never started): nothing to continue."""
    try:
        os.remove(_path(home))
    except OSError:
        pass
    shutil.rmtree(_media_dir(home), ignore_errors=True)
