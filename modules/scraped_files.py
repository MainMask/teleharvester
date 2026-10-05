"""The scrape's files in assets/databases: what the bot and the CLI offer to pick from,
and where an analysis of one writes."""
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from scraper.datafiles import read_table
from scraper.scrape import group_channel

BASES_DIR = "assets/databases"  # default scrape output; holds the <name>_participants bases


def _newest(pattern: str, limit: int, skip=lambda path: False) -> list[tuple[Path, int]]:
    """(path, row count) of BASES_DIR's newest `pattern` files; the count comes from the
    parquet footer, so even a large file isn't read."""
    try:
        paths = [p for p in Path(BASES_DIR).glob(pattern) if not skip(p)]
    except OSError:
        return []

    found = []
    for path in sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            found.append((path, pq.ParquetFile(path).metadata.num_rows))
        except Exception:  # unreadable / half-written file: not offered
            continue
    return found


def _rows(rows: int) -> str:
    return f"{rows:,}".replace(",", " ")


def _scraped_files(marker: str, limit: int = 10) -> list[tuple[str, str]]:
    """(path, label) of the scrape's <name><marker>… files in BASES_DIR, newest first.

    label: "OmniBase · 01.05.2022-27.09.2026 · 48 213".
    """
    bases = []
    # a verify result (<posts>_missed) is no scrape output
    for path, rows in _newest(f"*{marker}*.parquet", limit, skip=lambda p: p.stem.endswith("_missed")):
        name, _, span = path.stem.partition(marker)
        parts = [name, span.strip("_"), _rows(rows)]
        bases.append((str(path), " · ".join(part for part in parts if part)))
    return bases


def participant_bases(limit: int = 10) -> list[tuple[str, str]]:
    """The scraped recipient bases (for the mailing / adding to contacts)."""
    return _scraped_files("_participants", limit)


def posts_bases(limit: int = 10) -> list[tuple[str, str]]:
    """The scraped posts files (for the verify)."""
    return _scraped_files("_posts", limit)


def data_files(limit: int = 20) -> list[tuple[str, str]]:
    """(path, "file name · N строк") of every .parquet in BASES_DIR, newest first."""
    return [(str(path), f"{path.stem} · {_rows(rows)} строк") for path, rows in _newest("*.parquet", limit)]


def output_path(input_path: str, suffix: str) -> str:
    """Where an analysis of input_path writes, next to it and without an extension (the
    analysis adds it): OmniBase_posts_01.05.2022-27.09.2026 -> OmniBase_<suffix>_01.05…"""
    path = Path(input_path)
    name, marker, span = path.stem.partition("_posts")
    return str(path.with_name(f"{name}_{suffix}{span}" if marker else f"{path.stem}_{suffix}"))


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
        channels = [group_channel(group) for group in groups]
        window = df.attrs.get("scrape_window")
        return channels, (window["date_min"], window["date_max"]) if window else None
    except Exception:  # a folder, a glob, an unreadable file: everything is asked
        return [], None
