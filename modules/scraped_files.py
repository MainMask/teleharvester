"""The scrape's files in assets/databases: what the bot and the CLI offer to pick from,
where an analysis of one writes, and which scrapes are left to continue."""
import glob
import os
import re
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from modules import json_file
from scraper.datafiles import read_table
from scraper.scrape import _channel_ref, group_channel

BASES_DIR = "assets/databases"  # default scrape output; holds the <name>_participants bases
# a bot scrape in progress, continued after a bot restart (bot/routers/scraping/scrape.py)
SCRAPE_MARKER = os.path.join("stats", "bot_scrape.json")
# 🧩 Объединить's file of a project's posts files: <name>_posts_combined (see combined_output)
COMBINED = "_combined"
# what a scrape's file names are cut at (project_files, pending_resume)
FILE_MARKERS = ("_posts", "_reactors", "_participants", "_partial", "_previous", COMBINED)
# the analysis tools' outputs, named after the posts file: <name>_<suffix><span>… (see output_path)
ANALYSIS_SUFFIXES = ("links", "keywords", "comments", "summary", "sample")
_SPAN = re.compile(r"_\d{2}\.\d{2}\.\d{4}-\d{2}\.\d{2}\.\d{4}")


def name_problem(name: str) -> str | None:
    """Why `name` can't name a scrape's files in BASES_DIR (scrape, members, project), or None."""
    if not name:
        return "имя пустое"
    if "/" in name or "\\" in name or name in (".", ".."):
        return "имя станет частью имени файла: без / и \\"
    if marker := next((m for m in FILE_MARKERS if m in name), None):
        return f"в имени не должно быть «{marker}» — так размечаются файлы скрапа"
    return None


def _newest(pattern: str, limit: int, skip=lambda path: False) -> list[tuple[Path, int]]:
    """(path, row count) of BASES_DIR's newest `pattern` files; the count comes from the
    parquet footer, so even a large file isn't read."""
    try:
        paths = [p for p in Path(BASES_DIR).glob(pattern) if not skip(p)]
    except OSError:
        return []

    def mtime(path):
        try:
            return path.stat().st_mtime
        except OSError:  # removed since the glob: its footer read below drops it
            return 0

    found = []
    for path in sorted(paths, key=mtime, reverse=True)[:limit]:
        try:
            found.append((path, pq.ParquetFile(path).metadata.num_rows))
        except Exception:  # unreadable / half-written file: not offered
            continue
    return found


def row_count(path) -> int:
    """How many rows the parquet file has (its footer: never read whole); 0 without one."""
    try:
        return pq.ParquetFile(path).metadata.num_rows
    except Exception:  # none, half-written, unreadable
        return 0


def _rows(rows: int) -> str:
    return f"{rows:,}".replace(",", " ")


def scrape_names(limit: int = 30) -> list[str]:
    """The names the scrape's files in BASES_DIR are under (a scrape's, a members base's), newest first."""
    names = []
    for path, _ in _newest("*.parquet", limit * 5, skip=lambda p: p.stem.endswith("_missed")):
        for marker in ("_posts", "_reactors", "_participants"):
            name, found, _ = path.stem.partition(marker)
            if found:
                if name not in names:
                    names.append(name)
                break
    return names[:limit]


def members_base(name: str) -> Path:
    """The members' base of `name` (scraper/members.py names it so): a project's when named after it."""
    return Path(BASES_DIR) / f"{name}_participants_members.parquet"


def list_path(name: str) -> Path:
    """A project's uploaded list (modules/projects.add_to_list): one @username or +phone a line."""
    return Path(BASES_DIR) / f"{name}_participants_list.txt"


def read_list(path) -> list[str]:
    """The list's lines; none without the file."""
    try:
        return Path(path).read_text(encoding="utf-8").split()
    except FileNotFoundError:
        return []


def project_files(name: str, limit: int = 50, kinds=("posts", "reactors", "participants")) -> list[tuple[str, str]]:
    """(path, label) of a project's scrape files in BASES_DIR, newest first: the posts, reactors
    and participants files (or only `kinds` of them) whose name before the marker is the project's
    exactly (GreenBase's, not GreenBase2's). label: "posts · 01.05.2022-27.09.2026 · 48 213".
    "list" in `kinds`: its uploaded list too, last ("list · 120") — a text file, for the mailing only."""
    def kind(path):  # "posts" / "reactors" / "participants"; None for another name's (Green_x's for Green)
        for marker in ("_posts", "_reactors", "_participants"):
            prefix, found, _ = path.stem.partition(marker)
            if found:
                return marker[1:] if prefix == name else None
        return None

    files = []
    # skipped before the limit, which then counts the files asked for only
    for path, rows in _newest(f"{glob.escape(name)}_*.parquet", limit,
                              skip=lambda p: p.stem.endswith("_missed") or kind(p) not in kinds):
        marker = kind(path)
        parts = [marker, path.stem.partition(f"_{marker}")[2].strip("_"), _rows(rows)]
        files.append((str(path), " · ".join(part for part in parts if part)))
    if "list" in kinds and (path := list_path(name)).exists():
        files.append((str(path), f"list · {_rows(len(read_list(path)))}"))
    return files


def scrape_posts(name: str) -> list[tuple[str, str]]:
    """The posts files the project's scrapes wrote (project_files' "posts"), its combined one left out:
    the files a top-up continues and 🧩 Объединить merges."""
    return [(path, label) for path, label in project_files(name, kinds=("posts",))
            if not Path(path).stem.endswith(COMBINED)]


def combined_output(name: str) -> str:
    """Where 🧩 Объединить writes a project's posts files merged (save_table adds .parquet): one of its posts
    files, offered on its card as "posts · combined"."""
    return str(Path(BASES_DIR) / f"{name}_posts{COMBINED}")


def project_entries(name: str) -> list[Path]:
    """Everything of a project in BASES_DIR (its top level), for renaming it: the scrape's files (a
    verify's _missed too), the analysis outputs, the <name>_partial and <name>_previous folders.
    A file is the project's when a marker follows its name at once — never another name that
    starts with it: Green_x's files, or a one-off scrape Green_links (Green_links_posts_…), for Green."""
    try:
        paths = list(Path(BASES_DIR).glob(f"{glob.escape(name)}_*"))
    except OSError:
        return []
    found = []
    for path in paths:
        rest = path.name[len(name) + 1:]
        if path.is_dir():
            if rest in ("partial", "previous"):
                found.append(path)
            continue
        token = re.match(r"[a-z]+", rest)
        if token is None:
            continue
        after = rest[token.end():]
        if token.group() in ("posts", "reactors", "participants") and after[:1] in ("_", ".", ""):
            found.append(path)  # these markers can't be in a name: the file is surely this name's
        # an analysis of a posts file: its span, or its combined one's marker (no name holds it), follows
        elif token.group() in ANALYSIS_SUFFIXES and (_SPAN.match(after) or after.startswith(COMBINED)
                                                     or after[:1] in (".", "")):
            found.append(path)
    return sorted(found)


def output_path(input_path: str, suffix: str) -> str:
    """Where an analysis of input_path writes, next to it and without an extension (the
    analysis adds it): OmniBase_posts_01.05.2022-27.09.2026 -> OmniBase_<suffix>_01.05…"""
    path = Path(input_path)
    name, marker, span = path.stem.partition("_posts")
    return str(path.with_name(f"{name}_{suffix}{span}" if marker else f"{path.stem}_{suffix}"))


def missed_path(input_path: str, channel: str) -> str:
    """Where a verify of input_path against channel writes its missed posts: next to a posts
    file (<file>_missed.parquet); for a folder, in it, and for a glob, in its folder
    (<channel>_missed.parquet)."""
    path = Path(input_path)
    if path.is_file():
        return str(path.with_suffix("")) + "_missed.parquet"
    folder = path if path.is_dir() else path.parent
    return str(folder / f"{_channel_ref(channel).slug}_missed.parquet")


def unfinished_scrape(path: str) -> bool:
    """A scrape on the account (session path) is left to continue, on it only: the bot's marker, or
    a checkpoint in BASES_DIR (one stopped with ⏹, by an error or in the terminal menu has no marker)."""
    try:
        marker = json_file.load(SCRAPE_MARKER, None)
    except (OSError, ValueError):  # a half-written / broken marker: nothing to continue
        marker = None
    if marker is not None and marker.get("account") == path:
        return True
    for resume in Path(BASES_DIR).glob("*_partial/checkpoint/resume.json"):
        try:
            if json_file.load(resume, {}).get("account") == path:
                return True
        except (OSError, ValueError):  # a half-written / broken checkpoint: nothing to continue
            continue
    return False


def verify_presets(path: str) -> tuple[list[str], tuple[str, str] | None]:
    """(channels, window) to verify a posts file against: its Group column's channels and
    the scrape's own date window (YYYY-MM-DD), which the scrape stores in the file.
    ([], None) if the file can't tell; never the posts' span, which would hide a
    scrape cut short."""
    try:
        if Path(path).suffix.lower() == ".parquet":
            df = pd.read_parquet(path, columns=["Group"])  # the window rides in its metadata
        else:
            df = read_table(path)
        groups = df["Group"].dropna().astype(str).unique() if "Group" in df.columns else []
        # as the scrape was given them; an older or a combined file: rebuilt from its Group column
        channels = df.attrs.get("channels") or [group_channel(group) for group in groups]
        window = df.attrs.get("scrape_window")
        return channels, (window["date_min"], window["date_max"]) if window else None
    except Exception:  # a folder, a glob, an unreadable file: everything is asked
        return [], None
