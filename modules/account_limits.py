from datetime import date

from modules import json_file


class DailyCounter:
    """Per-account action counter for one action type, reset each day, persisted to `path`.

    A cap of 0 (or less) means unlimited: nothing is read or written, `reached` is always
    False and `bump` is a no-op, so the feature is inert until a cap is set in config.toml.
    """

    def __init__(self, path: str, cap: int):
        self.path = path
        self.cap = cap
        self.counts: dict = {}

        if self.cap and self.cap > 0:
            self._load()

    def _load(self):
        try:
            data = json_file.load(self.path, {})
        except (OSError, ValueError):
            data = {}

        # keep only today's counters; stale-date entries already count as 0, so dropping
        # them changes nothing but stops the file growing by one entry per account forever
        today = date.today().isoformat()
        self.counts = {
            key: entry for key, entry in data.items()
            if isinstance(entry, dict) and entry.get("date") == today
        }

    def reached(self, key) -> bool:
        if not self.cap or self.cap <= 0:
            return False

        entry = self.counts.get(str(key))
        today = date.today().isoformat()
        count = entry["count"] if entry and entry.get("date") == today else 0
        return count >= self.cap

    def bump(self, key):
        if not self.cap or self.cap <= 0:
            return

        today = date.today().isoformat()
        entry = self.counts.get(str(key))

        if not entry or entry.get("date") != today:
            entry = {"date": today, "count": 0}
            self.counts[str(key)] = entry

        entry["count"] += 1
        json_file.save(self.path, self.counts)
