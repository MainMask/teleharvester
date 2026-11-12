"""An account's age (modules/account_age.py): the seller's date or an estimate from its id; the seller's date read
from a .json and kept (modules/session_import.py); the warmup's setup spread over its days (modules/warmup.py)."""
import datetime
import json
import os

import pytest

from modules import account_age, session_import, warmup

D = datetime.date


@pytest.fixture
def points(monkeypatch):
    """Two known ids: 1000 of 2020-01-01, 2000 of 2020-01-21."""
    monkeypatch.setattr(account_age, "_points", lambda: ([1000, 2000], [D(2020, 1, 1).toordinal(),
                                                                         D(2020, 1, 21).toordinal()]))


def test_an_id_s_date_is_interpolated_between_its_neighbours_and_past_the_ends_is_the_end_s(points):
    assert account_age.estimate(1500) == D(2020, 1, 11)
    assert account_age.estimate(2000) == D(2020, 1, 21)
    assert account_age.estimate(10) == D(2020, 1, 1)  # older than the first known one
    assert account_age.estimate(5000) == D(2020, 1, 21)  # newer than the last: no earlier than it


def test_the_dataset_loads_and_grows_with_the_id():
    assert account_age.estimate(1) < D(2014, 1, 1) < account_age.estimate(1_000_000_000) < account_age.estimate(
        8_000_000_000)


def test_the_seller_s_date_wins_over_the_estimate(points):
    sold = datetime.datetime(2019, 3, 5).timestamp()
    assert account_age.registered(sold, 1500) == (D(2019, 3, 5), True)
    assert account_age.registered(None, 1500) == (D(2020, 1, 11), False)
    assert account_age.registered(None, None) is None


def test_the_label_says_years_and_months_an_estimate_with_a_tilde(points):
    today = D(2022, 4, 20)
    assert account_age.label(datetime.datetime(2019, 3, 5).timestamp(), None, today) == "3 г. 1 мес."
    assert account_age.label(None, 1000, today) == "~2 г. 3 мес."
    assert account_age.label(None, 1000, D(2021, 1, 1)) == "~1 г."
    assert account_age.label(None, 1000, D(2020, 1, 15)) == "~меньше месяца"
    assert account_age.label(None, None, today) == ""


def test_an_id_newer_than_the_dataset_is_new_however_old_the_dataset_gets(points):
    later = D(2030, 1, 1)  # years after the dataset's last date: its id still says nothing older
    assert account_age.label(None, 5000, later) == "новый, после 01.2020"
    assert account_age.label(None, 2000, later) == "~9 г. 11 мес."


@pytest.mark.parametrize("value, expected", [
    (1_600_000_000, 1_600_000_000.0),                 # seconds
    (1_600_000_000_000, 1_600_000_000.0),             # milliseconds
    ("1600000000", 1_600_000_000.0),
    ("2020-09-13", datetime.datetime(2020, 9, 13).timestamp()),
    ("вчера", None), (None, None), (True, None),
    (1_000_000, None),                                # before Telegram
    (4_000_000_000, None),                            # in the future
])
def test_the_seller_s_register_time_is_read_in_any_usual_form(value, expected):
    assert session_import.registered_at({"register_time": value}) == expected


def test_the_seller_s_date_goes_into_the_export_and_reads_back(tmp_path):
    from modules.types.account import Account
    from modules.types.account_settings import AccountSettings
    from modules.types.application import Application
    from telethon.crypto import AuthKey
    from telethon.sessions import StringSession

    session = StringSession()
    session.set_dc(2, "149.154.167.51", 443)
    session.auth_key = AuthKey(b"k" * 256)
    path = str(tmp_path / "79991234567.jsession")
    AccountSettings(session.save(), Account("Ann", None, 7, 0.0, "79991234567", "ann"),
                    Application(2040, "hash", "PC 64bit", "5.1", "Windows 10", "tdesktop", "ru"), None, None,
                    1_600_000_000.0).save(path)
    out = tmp_path / "out"
    out.mkdir()

    session_import.export(path, str(out))

    with open(out / "79991234567.json", encoding="utf-8") as fileobj:
        meta = json.load(fileobj)
    assert meta["register_time"] == 1_600_000_000 and session_import.registered_at(meta) == 1_600_000_000.0


def test_the_setup_is_the_security_first_then_the_profile_spread_over_the_days():
    settings = {**warmup.DEFAULTS, "days": 5, "steps": dict.fromkeys(warmup.STEPS, warmup.ON)}
    # 5 days: the security on the first, the 6 profile steps over the 4 left — 2, 2, 1, 1
    done, days = [], []
    for day in range(5):
        today = warmup.setup_today(settings, done, day)
        days.append(today)
        done += today
    assert days == [["lastseen", "2fa", "terminate"], ["name", "username"], ["photo", "bio"], ["music"],
                    ["stickers"]]
    # a security step that failed (other sessions, refused a fresh one for 24 h): the next day, with the profile's share
    assert warmup.setup_today(settings, ["lastseen", "2fa"], 1) == ["terminate", "name", "username"]
    # one failing every day holds the profile up no day: it goes on, the failing one tried along
    done, days = ["lastseen", "terminate"], []
    for day in range(1, 5):
        today = warmup.setup_today(settings, done, day)
        days.append(today)
        done += [step for step in today if step != "2fa"]
    assert all(today[0] == "2fa" for today in days) and set(done) == set(warmup.SETUP) - {"2fa"}
    # past the days, all that is left
    assert warmup.setup_today(settings, list(warmup.SECURITY_KEYS), 6) == \
        ["name", "username", "photo", "bio", "music", "stickers"]
    settings["steps"]["2fa"] = warmup.OFF  # an off one is no step
    assert warmup.setup_today(settings, [], 0) == ["lastseen", "terminate"]
    # a single day: all at once
    assert warmup.setup_today({**settings, "days": 1}, [], 0) == \
        ["lastseen", "terminate", "name", "username", "photo", "bio", "music", "stickers"]  # the security first


# --- the weekly update of the table (account_age.validate / update, bot/services/id_dates_update.py) ---

def _bundled() -> list:
    with open(account_age.DATA_PATH, encoding="utf-8") as fileobj:
        return json.load(fileobj)


def _newer() -> list:
    """The bundled table and a point past its last one."""
    return _bundled() + [{"id": "9999999999", "date": datetime.date.today().isoformat()}]


@pytest.mark.parametrize("entries, why", [
    ({"id": "1"}, "не список"),
    ([{"id": "1", "date": "2020-01-01"}] * 10, "не список"),  # too few
    ([{"id": "x", "date": "2020-01-01"}] * 200, "неверная запись"),
    ([{"id": "1"}] * 200, "неверная запись"),
    ([{"id": "-1", "date": "2020-01-01"}] * 200, "невозможная запись"),
    ([{"id": "1", "date": "2012-01-01"}] * 200, "невозможная запись"),
    ([{"id": "1", "date": "2999-01-01"}] * 200, "невозможная запись"),
])
def test_an_implausible_table_is_refused(entries, why):
    with pytest.raises(ValueError, match=why):
        account_age.validate(entries)


def test_a_table_fewer_older_or_reaching_a_lower_id_than_the_one_in_use_is_refused():
    current = account_age.validate(_bundled())
    with pytest.raises(ValueError, match="старее или меньше"):
        account_age.validate(_bundled()[:-1], current)  # a point fewer
    lower = [*_bundled(), {"id": "5", "date": "2013-09-01"}]  # more points, but its newest no newer, its top id not
    assert account_age.validate(lower, current)  # the same reach, a point more: fine
    with pytest.raises(ValueError, match="старее или меньше"):
        account_age.validate([{**entry, "id": str(int(entry["id"]) // 2)} for entry in lower], current)


def test_a_newer_table_is_kept_and_used_at_once_the_same_one_changes_nothing():
    before = account_age.estimate(9_999_999_999)
    assert account_age.update(json.dumps(_bundled()).encode()) is None  # the same: nothing written
    assert not os.path.exists(account_age.UPDATED_PATH)

    told = account_age.update(json.dumps(_newer()).encode())
    assert told.startswith(f"таблица возрастов обновлена: {len(_newer())} точек, до ")
    assert os.path.exists(account_age.UPDATED_PATH)
    assert account_age.estimate(9_999_999_999) == datetime.date.today() != before  # no restart needed
    assert not account_age.newer(9_999_999_999)


def test_a_broken_or_older_kept_table_falls_back_to_the_bundled_one():
    bundled = account_age.validate(_bundled())
    os.makedirs(os.path.dirname(account_age.UPDATED_PATH), exist_ok=True)
    for text in ("{not json", json.dumps(_bundled()[:-5])):
        with open(account_age.UPDATED_PATH, "w", encoding="utf-8") as fileobj:
            fileobj.write(text)
        account_age._points.cache_clear()
        assert account_age._points() == bundled


def test_a_check_logs_what_it_did_and_a_failure_is_tried_again_sooner(caplog):
    import asyncio
    import logging

    from bot.services import id_dates_update

    async def offline():
        raise OSError("нет сети")

    async def garbage():
        return b"<html>"

    async def newer():
        return json.dumps(_newer()).encode()

    caplog.set_level(logging.INFO, logger=id_dates_update.__name__)
    assert asyncio.run(id_dates_update.check_once(offline)) is False
    assert asyncio.run(id_dates_update.check_once(garbage)) is True  # refused: no better in 6 h, a week then
    assert asyncio.run(id_dates_update.check_once(newer)) is True
    assert asyncio.run(id_dates_update.check_once(newer)) is True  # the same now
    assert [record.getMessage() for record in caplog.records] == [
        "таблица возрастов не скачана: нет сети",
        "таблица возрастов отклонена: Expecting value: line 1 column 1 (char 0)",
        f"таблица возрастов обновлена: {len(_newer())} точек, до {datetime.date.today():%d.%m.%Y}",
        "таблица возрастов актуальна",
    ]


def test_a_session_file_written_now_is_read_by_the_bot_before_this_change_too(tmp_path):
    """A rollback keeps every worker: the bot before read `account` strictly (Account(**…), these six fields), and
    leaves a key beside it unread — the seller's date is kept there, not in `account`."""
    from modules.types.account import Account
    from modules.types.account_settings import AccountSettings
    from modules.types.application import Application

    path = str(tmp_path / "79991234567.jsession")
    AccountSettings("key", Account("Ann", None, 7, 0.0, "79991234567", "ann"),
                    Application(2040, "hash", "PC 64bit", "5.1", "Windows 10", "tdesktop", "ru"), None, None,
                    1_600_000_000.0).save(path)
    with open(path, encoding="utf-8") as fileobj:
        saved = json.load(fileobj)
    assert set(saved["account"]) == {"first_name", "last_name", "user_id", "added_at", "phone_number", "username"}
    assert saved["registered_at"] == 1_600_000_000.0
    # and a file of before (no date) reads as ever
    del saved["registered_at"]
    assert AccountSettings.from_dict(saved).registered_at is None
