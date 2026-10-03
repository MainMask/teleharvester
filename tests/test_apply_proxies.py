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

    assert summary == {"accounts": 4, "proxies_used": 2, "string_sessions_skipped": 0}

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
