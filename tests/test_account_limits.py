"""DailyCounter: per-account daily action caps (0 = off), reset by date, persisted."""

import json

from modules.account_limits import DailyCounter


def test_cap_zero_is_inert_and_writes_nothing(tmp_path):
    path = tmp_path / "limits.json"
    counter = DailyCounter(str(path), 0)

    assert counter.reached("acc") is False
    counter.bump("acc")
    counter.bump("acc")

    assert counter.reached("acc") is False
    assert not path.exists()  # disabled: no file touched


def test_reached_after_cap_bumps(tmp_path):
    counter = DailyCounter(str(tmp_path / "limits.json"), 2)

    assert counter.reached("acc") is False
    counter.bump("acc")
    assert counter.reached("acc") is False
    counter.bump("acc")
    assert counter.reached("acc") is True  # 2 >= cap 2

    # a different account is tracked separately
    assert counter.reached("other") is False


def test_counts_persist_across_instances(tmp_path):
    path = str(tmp_path / "limits.json")
    DailyCounter(path, 3).bump("acc")
    DailyCounter(path, 3).bump("acc")

    assert DailyCounter(path, 3).reached("acc") is False  # 2 < 3
    DailyCounter(path, 3).bump("acc")
    assert DailyCounter(path, 3).reached("acc") is True   # 3 >= 3


def test_stale_date_entries_reset(tmp_path):
    path = tmp_path / "limits.json"
    path.write_text(json.dumps({"acc": {"date": "2000-01-01", "count": 99}}))

    counter = DailyCounter(str(path), 5)
    assert counter.reached("acc") is False  # yesterday's count is dropped
