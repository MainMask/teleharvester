"""📚 Вся база: a project's base as one, each person once — its scrapes' participants, its groups' members and its
own list (📄 Свой список), glued at the moment they are used, so a new scrape is in it at once and no copy of the
same people lies on disk. The mailing, 📇 В контакты, 🎯's count and 📦 Скачать базу take it."""
import csv
import os

from modules import parquet_db, profile_done, scraped_files

WHOLE = "whole"  # the mailing's and the contacts' choice of it, in place of a file's path
EXPORT_COLUMNS = ("user_id", "access_hash", "owner_id", "username", "name", "phone")


def sources(name: str) -> list[tuple[str, str]]:
    """(path, label) of the files the project's base is made of, newest first: the participants' bases, then its
    list (scraped_files.project_files)."""
    return scraped_files.project_files(name, kinds=("participants", "list"))


def _username(value) -> str | None:
    """A username as compared across the files (@Alice = alice); None for none, a phone or an id."""
    name = str(value or "").strip().lstrip("@").casefold()
    return name if name and not name.lstrip("+").isdigit() else None


def merged(name: str, owners=frozenset(), only=None) -> list:
    """The project's people, each once: its bases' rows (dicts, as parquet_db.load gives them), then its list's
    lines no row is (strings, as the mailing reads a list). A person in several bases keeps the row whose access hash
    one of `owners` (the project's workers' ids) can use, else one with no owner, else the newest file's; with
    the username of another of their rows when it has none, so a worker the hash isn't for can still reach them.
    `only`: the rows' columns to read (parquet_db.load), all by default."""
    rows: dict[int, dict] = {}
    usernames: dict[int, str] = {}
    lines = []
    for path, _ in sources(name):  # newest first: an older file's row replaces none of equal rank
        if path.endswith(".txt"):
            lines = scraped_files.read_list(path)
            continue
        try:
            found = parquet_db.load(path, only)
        except (OSError, ValueError):  # gone meanwhile, or no base (no user_id / access_hash): not the people's
            continue
        for row in found:
            user_id = row["user_id"]
            if user_id is None or row.get("access_hash") is None:
                continue
            if row.get("username"):
                usernames.setdefault(user_id, row["username"])
            if user_id not in rows or _rank(row, owners) < _rank(rows[user_id], owners):
                rows[user_id] = row
    people = [{**row, "username": row.get("username") or usernames.get(user_id)} for user_id, row in rows.items()]
    known = {_username(row["username"]) for row in people} - {None}
    return people + [line for line in lines if _username(line) is None or _username(line) not in known]


def worker_ids(storage) -> set[int]:
    """The ids of the project's workers (`storage`: its sessions), merged's `owners`."""
    keys = (profile_done.worker_key(storage, path) for path in storage.jsessions_paths)
    return {int(key) for key in keys if key}


def _rank(row: dict, owners) -> int:
    """0: a row a worker of the project can use; 1: one of no known owner; 2: another account's."""
    owner = row.get("owner_id")
    return 0 if owner in owners else 1 if owner is None else 2


_COUNTED = ("user_id", "access_hash", "owner_id", "username")  # what merged decides by: all a count reads
_counts: dict = {}  # name -> (its files as they were, (people, with an id)): the card counts at every opening


def people(name: str, with_id: bool = False) -> int:
    """How many people the project's base has, each once (merged's count); `with_id`: only those with an id (the
    contacts': no line of its list). Read again only once its files change."""
    files = []
    for path, _ in sources(name):
        try:
            stat = os.stat(path)
        except OSError:  # gone meanwhile: merged leaves it out too
            continue
        files.append((path, stat.st_mtime_ns, stat.st_size))
    cached = _counts.get(name)
    if cached is None or cached[0] != files:
        found = merged(name, only=_COUNTED)
        cached = _counts[name] = (files, (len(found), sum(1 for person in found if isinstance(person, dict))))
    return cached[1][with_id]


def export(name: str, path: str) -> int:
    """The project's base, each person once, written to `path` as a CSV (Excel opens it: utf-8 with a BOM); a list's
    line has its username or its phone only. Returns how many people."""
    found = merged(name)
    with open(path, "w", encoding="utf-8-sig", newline="") as fileobj:
        writer = csv.DictWriter(fileobj, EXPORT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for person in found:
            if isinstance(person, dict):
                writer.writerow({column: "" if person.get(column) is None else person[column]
                                 for column in EXPORT_COLUMNS})
            elif _username(person):
                writer.writerow({"username": person})
            else:
                writer.writerow({"phone": person})
    return len(found)
