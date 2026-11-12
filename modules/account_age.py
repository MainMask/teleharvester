"""An account's age, for its card and 📋 Список: information only. Its registration date: the seller's (its
.json's register_time, kept as the .jsession's registered_at, modules/session_import.py), exact; else estimated
from its user id.

Telegram tells no account's registration date, but its user ids grew with time: an id's date is interpolated
between the known ones of telegram_id_dates.json (next to this file) — the dataset of
https://github.com/Jobians/telegram-id-age (MIT License, © 2025 JOBIANSTECHIE), as its estimator does. It is off
by 2–6 weeks for the accounts of 2013–2023, by 2–3.5 months (up to 10) for newer ones, whose ids are no longer
given out strictly in order: enough to tell a year from five, not a month from three.

The dataset grows: the bot downloads it weekly (bot/services/id_dates_update.py) into UPDATED_PATH, used once it
is checked (validate) and no older than the one next to this file, which stays the fallback.
"""
import bisect
import datetime
import json
import os
from functools import cache

from modules import atomic_file

DATA_PATH = os.path.join(os.path.dirname(__file__), "telegram_id_dates.json")  # the bundled one
UPDATED_PATH = os.path.join("stats", "telegram_id_dates.json")  # the weekly update's
FIRST_DAY = datetime.date(2013, 8, 1)  # no account is older
MIN_POINTS = 100


def validate(entries, current: tuple[list[int], list[int]] | None = None) -> tuple[list[int], list[int]]:
    """The dataset's `entries` ([{"id": "123", "date": "2020-01-31"}, …]) as points — (ids, their dates' ordinals),
    by id — if they are plausible and, given the `current` points, no fewer, no older, reaching no lower id;
    ValueError saying why not."""
    if not isinstance(entries, list) or len(entries) < MIN_POINTS:
        raise ValueError(f"не список из {MIN_POINTS}+ записей")
    latest = datetime.date.today() + datetime.timedelta(days=1)
    points = []
    for entry in entries:
        try:
            user_id, date = int(entry["id"]), datetime.date.fromisoformat(entry["date"])
        except (TypeError, KeyError, ValueError):
            raise ValueError(f"неверная запись: {str(entry)[:80]}") from None
        if user_id < 0 or not FIRST_DAY <= date <= latest:
            raise ValueError(f"невозможная запись: {str(entry)[:80]}")
        points.append((user_id, date.toordinal()))
    points.sort()
    ids, days = [user_id for user_id, _ in points], [day for _, day in points]
    if current is not None and (len(ids) < len(current[0]) or max(days) < max(current[1])
                                or ids[-1] < current[0][-1]):
        raise ValueError(f"старее или меньше текущей ({len(ids)} точек против {len(current[0])})")
    return ids, days


def _load(path: str):
    with open(path, encoding="utf-8") as fileobj:
        return json.load(fileobj)


@cache
def _points() -> tuple[list[int], list[int]]:
    """(ids, their dates' ordinals), by id: the update's when it is good and no older than the bundled one (code
    updated since may bring a newer one), else the bundled one."""
    bundled = validate(_load(DATA_PATH))
    try:
        return validate(_load(UPDATED_PATH), bundled)
    except (OSError, ValueError):  # none yet, broken, or older than the bundled one
        return bundled


def update(raw: bytes) -> str | None:
    """A downloaded dataset kept (UPDATED_PATH) and used at once, if it is good and differs from the one in use:
    what to log; None if it is the same. ValueError if it is no good (validate), OSError if it can't be kept."""
    current = _points()
    found = validate(json.loads(raw), current)
    if found == current:
        return None
    os.makedirs(os.path.dirname(UPDATED_PATH), exist_ok=True)
    atomic_file.write(UPDATED_PATH, raw.decode("utf-8"))
    _points.cache_clear()
    return (f"таблица возрастов обновлена: {len(found[0])} точек, "
            f"до {datetime.date.fromordinal(max(found[1])):%d.%m.%Y}")


def newer(user_id: int | None) -> bool:
    """An id above the dataset's last one: registered after its date — how long after, unknown."""
    return bool(user_id) and int(user_id) > _points()[0][-1]


def estimate(user_id: int) -> datetime.date:
    """The id's registration date interpolated between its neighbours in the dataset; past its ends, the end's
    date (an id above its last one: registered no earlier than that, see newer)."""
    ids, days = _points()
    k = bisect.bisect_left(ids, user_id)
    if k == 0:
        return datetime.date.fromordinal(days[0])
    if k == len(ids):
        return datetime.date.fromordinal(days[-1])
    (id0, day0), (id1, day1) = (ids[k - 1], days[k - 1]), (ids[k], days[k])
    return datetime.date.fromordinal(round(day0 + (day1 - day0) * (user_id - id0) / (id1 - id0)))


def registered(registered_at: float | None, user_id: int | None) -> tuple[datetime.date, bool] | None:
    """(the registration date, True if it is the seller's, exact); None with neither a date nor an id."""
    if registered_at:
        return datetime.date.fromtimestamp(registered_at), True
    if user_id:
        return estimate(int(user_id)), False
    return None


def label(registered_at: float | None, user_id: int | None, today: datetime.date | None = None) -> str:
    """"2 г. 3 мес." (the seller's date), "~8 мес." (estimated), "новый, после 09.2026" (an id newer than the
    dataset); "" if unknown."""
    found = registered(registered_at, user_id)
    if found is None:
        return ""
    date, exact = found
    if not exact and newer(user_id):
        return f"новый, после {date:%m.%Y}"
    today = today or datetime.date.today()
    months = max((today.year - date.year) * 12 + today.month - date.month - (today.day < date.day), 0)
    years, months = divmod(months, 12)
    text = " ".join(part for part in (f"{years} г." if years else "", f"{months} мес." if months else "") if part)
    return ("" if exact else "~") + (text or "меньше месяца")

