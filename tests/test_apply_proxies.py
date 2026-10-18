"""SessionsStorage.apply_proxies: distribute proxies across .jsession accounts (3 per proxy),
persist them, and rebuild each client with the new proxy."""

import json

import pytest
from telethon.sessions import StringSession

from modules.storages.sessions_storage import SessionsStorage
from modules.types.proxy import Proxy


def _write_jsession(directory, phone):
    data = {
        "auth_key": StringSession().save(),
        "account": {"first_name": "A", "last_name": "B", "user_id": int(phone),
                    "added_at": 0, "phone_number": phone},
        "application": {"api_id": 2040, "api_hash": "h", "device_name": "Desktop",
                        "app_version": "4.0", "sdk": "Windows 10", "lang_pack": "tdesktop",
                        "system_lang_code": "en"},
        "proxy": None,
        "password": None,
    }
    (directory / f"{phone}.jsession").write_text(json.dumps(data))


def _storage(tmp_path, count):
    for i in range(count):
        _write_jsession(tmp_path, f"100000000{i}")
    return SessionsStorage(str(tmp_path), 2040, "h", initialize=False)


def test_apply_proxies_distributes_three_per_proxy(tmp_path):
    storage = _storage(tmp_path, 4)
    proxies = [Proxy("socks5", "10.0.0.0", 1080), Proxy("socks5", "10.0.0.1", 1080)]

    summary = storage.apply_proxies(proxies)

    assert summary == {"accounts": 4, "proxies_used": 2, "restricted": 0, "string_sessions_skipped": 0}

    paths = sorted(storage.jsessions_paths)
    ips = [json.loads(open(p).read())["proxy"]["ip"] for p in paths]
    assert ips == ["10.0.0.0", "10.0.0.0", "10.0.0.0", "10.0.0.1"]

    # clients were rebuilt with the new proxy
    assert storage.full_sessions[paths[0]]._proxy == proxies[0].as_telethon()
    assert storage.full_sessions[paths[3]]._proxy == proxies[1].as_telethon()


def test_apply_proxies_errors_when_too_few(tmp_path):
    storage = _storage(tmp_path, 4)
    with pytest.raises(ValueError):
        storage.apply_proxies([Proxy("socks5", "10.0.0.0", 1080)])


def test_add_jsession_registers_and_dedupes(tmp_path):
    _write_jsession(tmp_path, "1000000000")
    storage = SessionsStorage(str(tmp_path), 2040, "h", initialize=False)
    assert len(storage.jsessions_paths) == 1

    # a brand-new account registers
    _write_jsession(tmp_path, "1000000001")
    added = storage.add_jsession(str(tmp_path / "1000000001.jsession"))
    assert added is not None
    assert len(storage.jsessions_paths) == 2
    assert str(tmp_path / "1000000001.jsession") in storage.full_sessions

    # same phone again (different file name) -> rejected as duplicate
    _write_jsession(tmp_path, "1000000009")
    dup_path = tmp_path / "1000000009.jsession"
    data = json.loads(dup_path.read_text())
    data["account"]["phone_number"] = "1000000001"
    dup_path.write_text(json.dumps(data))
    assert storage.add_jsession(str(dup_path)) is None


def test_the_proxy_pool_is_owner_only(tmp_path, monkeypatch):
    """Its lines hold the proxies' logins and passwords: as a session's key, never readable by others."""
    import os

    from modules import tdata_import

    path = tmp_path / "proxies.txt"
    path.write_text("socks5://old:pw@10.0.0.1:1080\n")
    os.chmod(path, 0o644)  # a pool written before: narrowed on the next write too
    tdata_import.save_proxies("socks5://user:secret@10.0.0.2:1080", str(path))
    assert oct(os.stat(path).st_mode & 0o777) == oct(0o600)


# --- the permanently restricted workers' own proxies (projects.restricted_proxies_path) -----------

MAIN = [Proxy("socks5", "10.0.0.0", 1080), Proxy("socks5", "10.0.0.1", 1080)]
SPARE = [Proxy("socks5", "10.9.9.9", 1080)]


def _ips(storage):
    return {path: json.loads(open(path).read())["proxy"]["ip"] for path in sorted(storage.jsessions_paths)}


def test_apply_proxies_puts_the_restricted_on_their_own_proxies(tmp_path):
    storage = _storage(tmp_path, 4)
    restricted = {sorted(storage.jsessions_paths)[0]}

    summary = storage.apply_proxies(MAIN[:1], SPARE, restricted)  # 3 others: one pool proxy is enough

    assert summary["accounts"] == 3 and summary["restricted"] == 1 and summary["proxies_used"] == 1
    assert list(_ips(storage).values()) == ["10.9.9.9", "10.0.0.0", "10.0.0.0", "10.0.0.0"]


def test_rehome_moves_only_the_restricted_off_the_pool(tmp_path):
    storage = _storage(tmp_path, 4)
    storage.apply_proxies(MAIN)  # 3 on the first, 1 on the second
    paths = sorted(storage.jsessions_paths)
    before = _ips(storage)

    assert storage.rehome_restricted({paths[0]}, MAIN, SPARE, dry_run=True)["out"] == [paths[0]]
    assert _ips(storage) == before  # a dry run changes nothing

    moves = storage.rehome_restricted({paths[0]}, MAIN, SPARE)

    assert moves == {"out": [paths[0]], "back": [], "stuck": [], "busy": []}
    after = _ips(storage)
    assert after[paths[0]] == "10.9.9.9" and all(after[p] == before[p] for p in paths[1:])  # the others' IPs kept
    assert storage.full_sessions[paths[0]]._proxy == SPARE[0].as_telethon()  # its client rebuilt


def test_rehome_brings_one_clean_again_back_or_reports_it_stuck(tmp_path):
    storage = _storage(tmp_path, 4)
    storage.apply_proxies(MAIN[:1], SPARE, {sorted(storage.jsessions_paths)[0]})
    first = sorted(storage.jsessions_paths)[0]

    assert storage.rehome_restricted(set(), MAIN[:1], SPARE) == {"out": [], "back": [], "stuck": [first], "busy": []}  # pool full
    assert storage.rehome_restricted(set(), MAIN, SPARE) == {"out": [], "back": [first], "stuck": [], "busy": []}
    assert _ips(storage)[first] == "10.0.0.1"


def test_rehome_without_proxies_for_the_restricted_does_nothing(tmp_path):
    storage = _storage(tmp_path, 2)
    storage.apply_proxies(MAIN)
    assert storage.rehome_restricted({sorted(storage.jsessions_paths)[0]}, MAIN, []) == \
        {"out": [], "back": [], "stuck": [], "busy": []}


def test_rehome_moves_the_restricted_out_first_so_one_clean_again_takes_its_place(tmp_path):
    storage = _storage(tmp_path, 4)
    paths = sorted(storage.jsessions_paths)
    # paths[0] clean again on the spare proxy; paths[3] newly restricted, on the full pool proxy
    storage.apply_proxies(MAIN[:1], SPARE, {paths[0]})

    moves = storage.rehome_restricted({paths[3]}, MAIN[:1], SPARE)

    assert moves == {"out": [paths[3]], "back": [paths[0]], "stuck": [], "busy": []}  # not stuck: paths[3] freed a place
    assert _ips(storage)[paths[0]] == "10.0.0.0" and _ips(storage)[paths[3]] == "10.9.9.9"


def test_rehome_finds_the_ones_on_a_replaced_list(tmp_path):
    storage = _storage(tmp_path, 4)
    paths = sorted(storage.jsessions_paths)
    storage.apply_proxies(MAIN, SPARE, {paths[0], paths[1]})
    new = [Proxy("socks5", "10.8.8.8", 1080)]

    # paths[0] restricted still: onto the new list; paths[1] clean again: back, though the old list is gone
    moves = storage.rehome_restricted({paths[0]}, MAIN, new)  # the old list's proxies: off the pool still

    assert moves == {"out": [paths[0]], "back": [paths[1]], "stuck": [], "busy": []}
    assert _ips(storage)[paths[0]] == "10.8.8.8"


def test_a_pool_proxy_is_never_off_the_pool_and_a_foreign_one_is(tmp_path):
    """Off the pool = not on a pool proxy: one put back into the pool counts as a pool one at once, and
    a worker moved here from another project with that project's proxy is offered into this pool."""
    from functions import spamblock

    home = tmp_path / "home"
    home.mkdir()
    (home / "proxies.txt").write_text("socks5://10.0.0.0:1080\nsocks5://10.9.9.9:1080\n")  # an old spare, now pool
    _storage(tmp_path, 4)
    storage = SessionsStorage(str(tmp_path), 2040, "h", initialize=False, home=str(home))
    storage.apply_proxies(MAIN[:1] + SPARE)  # 3 on 10.0.0.0, 1 on 10.9.9.9
    assert spamblock.rehome(storage, dry_run=True) == {"out": [], "back": [], "stuck": [], "busy": []}

    moved_in = sorted(storage.jsessions_paths)[3]
    storage._set_proxy(moved_in, Proxy("socks5", "172.16.0.1", 1080))  # another project's
    assert spamblock.rehome(storage, dry_run=True) == {"out": [], "back": [moved_in], "stuck": [], "busy": []}


def test_rehome_leaves_a_busy_worker_as_it_is(tmp_path):
    storage = _storage(tmp_path, 4)
    storage.apply_proxies(MAIN)
    paths = sorted(storage.jsessions_paths)
    before = _ips(storage)

    moves = storage.rehome_restricted({paths[0], paths[1]}, MAIN, SPARE, busy={paths[0]})

    assert moves == {"out": [paths[1]], "back": [], "stuck": [], "busy": [paths[0]]}
    assert _ips(storage)[paths[0]] == before[paths[0]]  # its client in use: untouched


def test_with_no_list_for_them_the_restricted_come_back_too(tmp_path):
    storage = _storage(tmp_path, 4)
    paths = sorted(storage.jsessions_paths)
    storage.apply_proxies(MAIN[:1], SPARE, {paths[0]})

    moves = storage.rehome_restricted({paths[0]}, MAIN, [])  # still restricted, but not separated now

    assert moves["back"] == [paths[0]] and _ips(storage)[paths[0]] == "10.0.0.1"
