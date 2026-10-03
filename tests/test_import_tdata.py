"""tdata import: proxy loading, 2FA password reading, and import_all orchestration
(TDesktop/ToTelethon are mocked, so no network or real tdata is touched)."""

import asyncio
import json

import pytest

from modules import tdata_import


def test_load_proxies_missing_file(tmp_path):
    assert tdata_import.load_proxies(str(tmp_path / "nope.txt")) == []


def test_load_proxies_skips_blanks_and_comments(tmp_path):
    f = tmp_path / "proxies.txt"
    f.write_text("# comment\n\nsocks5://u:p@1.1.1.1:1080\nhttp://2.2.2.2:3128\n")
    proxies = tdata_import.load_proxies(str(f))
    assert [(x.proxy_type, x.ip, x.port) for x in proxies] == [
        ("socks5", "1.1.1.1", 1080),
        ("http", "2.2.2.2", 3128),
    ]


def test_read_2fa_password(tmp_path):
    (tmp_path / "Пароль 2фа dark.txt").write_text("Пароль 2фа dark: secret123\r\n", encoding="utf-8")
    assert tdata_import.read_2fa_password(str(tmp_path)) == "secret123"


def test_read_2fa_password_missing(tmp_path):
    assert tdata_import.read_2fa_password(str(tmp_path)) is None


class _FakeMe:
    first_name = "Denis"
    last_name = "A"
    id = 123
    phone = "79990001122"


class _FakeInit:
    device_model = "Desktop"
    system_version = "Windows 10"
    app_version = "3.4.3 x64"
    lang_pack = "tdesktop"
    system_lang_code = "en-US"


class _FakeSession:
    def save(self):
        return "AUTHKEY"


class _FakeClient:
    api_id = 2040
    api_hash = "hash"
    _init_request = _FakeInit()
    session = _FakeSession()

    async def connect(self):
        pass

    async def is_user_authorized(self):
        return True

    async def get_me(self):
        return _FakeMe()

    async def disconnect(self):
        pass


class _FakeTDesktop:
    def __init__(self, path):
        self.path = path

    async def ToTelethon(self, session, flag, proxy=None):
        _FakeTDesktop.last_proxy = proxy
        return _FakeClient()


def _setup_workers(tmp_path, monkeypatch, count=2, with_proxies=0):
    workers = tmp_path / "workers"
    sessions = tmp_path / "sessions"
    workers.mkdir()
    for i in range(count):
        w = workers / f"acc{i}"
        (w / "tdata").mkdir(parents=True)
        (w / "Пароль 2фа dark.txt").write_text(f"x: pw{i}\n", encoding="utf-8")

    proxies_path = tmp_path / "proxies.txt"
    if with_proxies:
        proxies_path.write_text(
            "".join(f"socks5://u:p@10.0.0.{i}:1080\n" for i in range(with_proxies))
        )

    monkeypatch.setattr(tdata_import, "TDesktop", _FakeTDesktop)
    return str(workers), str(sessions), str(proxies_path)


def test_import_all_writes_jsession(tmp_path, monkeypatch):
    workers, sessions, proxies = _setup_workers(tmp_path, monkeypatch, count=1, with_proxies=1)

    count = asyncio.run(tdata_import.import_all(workers, sessions, proxies))

    assert count == 1
    out = tmp_path / "sessions" / "79990001122.jsession"
    assert out.exists()
    data = json.loads(out.read_text())
    assert data["auth_key"] == "AUTHKEY"
    assert data["password"] == "pw0"
    assert data["proxy"]["ip"] == "10.0.0.0"
    assert data["application"]["device_name"] == "Desktop"
    # a second run skips the already-imported worker
    assert asyncio.run(tdata_import.import_all(workers, sessions, proxies)) == 0


def test_import_all_errors_when_too_few_proxies(tmp_path, monkeypatch):
    # 4 accounts need 2 proxies at 3-per-proxy, but only 1 is provided
    workers, sessions, proxies = _setup_workers(tmp_path, monkeypatch, count=4, with_proxies=1)

    with pytest.raises(RuntimeError):
        asyncio.run(tdata_import.import_all(workers, sessions, proxies))


def test_proxy_shared_by_three_accounts(tmp_path, monkeypatch):
    workers, sessions, proxies = _setup_workers(tmp_path, monkeypatch, count=4, with_proxies=2)

    seen = []

    async def fake_convert(tdata_dir, proxy, password, sessions_dir):
        seen.append(proxy.ip if proxy else None)
        return f"phone{len(seen)}"

    monkeypatch.setattr(tdata_import, "convert", fake_convert)

    count = asyncio.run(tdata_import.import_all(workers, sessions, proxies))

    assert count == 4
    # first three accounts share proxy #0, the fourth rolls onto proxy #1
    assert seen == ["10.0.0.0", "10.0.0.0", "10.0.0.0", "10.0.0.1"]


def test_import_all_no_proxies_file_imports_without_proxy(tmp_path, monkeypatch):
    workers, sessions, proxies = _setup_workers(tmp_path, monkeypatch, count=1, with_proxies=0)

    count = asyncio.run(tdata_import.import_all(workers, sessions, proxies))
    assert count == 1
    data = json.loads((tmp_path / "sessions" / "79990001122.jsession").read_text())
    assert data["proxy"] is None


def test_convert_connects_through_proxy(tmp_path, monkeypatch):
    from modules.types.proxy import Proxy
    monkeypatch.setattr(tdata_import, "TDesktop", _FakeTDesktop)
    proxy = Proxy("socks5", "10.0.0.9", 1080, "u", "p")

    asyncio.run(tdata_import.convert(str(tmp_path), proxy, None, str(tmp_path / "s")))

    assert _FakeTDesktop.last_proxy == proxy.as_telethon()


def test_import_all_counts_already_imported_accounts(tmp_path, monkeypatch):
    workers, sessions, proxies = _setup_workers(tmp_path, monkeypatch, count=1, with_proxies=2)
    # proxy #0 is already full with accounts from an earlier run
    (tmp_path / "sessions").mkdir()
    for i in range(3):
        (tmp_path / "sessions" / f"old{i}.jsession").write_text(json.dumps({
            "proxy": {"proxy_type": "socks5", "ip": "10.0.0.0", "port": 1080,
                      "user": "u", "password": "p"},
        }))

    assert asyncio.run(tdata_import.import_all(workers, sessions, proxies)) == 1
    data = json.loads((tmp_path / "sessions" / "79990001122.jsession").read_text())
    assert data["proxy"]["ip"] == "10.0.0.1"


def test_import_all_errors_when_existing_accounts_fill_proxies(tmp_path, monkeypatch):
    workers, sessions, proxies = _setup_workers(tmp_path, monkeypatch, count=1, with_proxies=1)
    (tmp_path / "sessions").mkdir()
    for i in range(3):
        (tmp_path / "sessions" / f"old{i}.jsession").write_text(json.dumps({
            "proxy": {"proxy_type": "socks5", "ip": "10.0.0.0", "port": 1080,
                      "user": "u", "password": "p"},
        }))

    with pytest.raises(RuntimeError):
        asyncio.run(tdata_import.import_all(workers, sessions, proxies))
    assert not (tmp_path / "sessions" / "79990001122.jsession").exists()


# --- find_tdata_dir / choose_pool_proxy ---

def test_find_tdata_dir_by_key_datas(tmp_path):
    nested = tmp_path / "somewhere" / "tdata"
    nested.mkdir(parents=True)
    (nested / "key_datas").write_bytes(b"x")
    assert tdata_import.find_tdata_dir(str(tmp_path)) == str(nested)


def test_find_tdata_dir_none(tmp_path):
    (tmp_path / "a").mkdir()
    assert tdata_import.find_tdata_dir(str(tmp_path)) is None


def test_choose_pool_proxy_empty_pool_returns_none():
    from modules.types.proxy import Proxy
    assert tdata_import.choose_pool_proxy([Proxy("socks5", "1.1.1.1", 1)], []) is None


def test_choose_pool_proxy_first_free():
    from modules.types.proxy import Proxy
    p0 = Proxy("socks5", "10.0.0.0", 1080)
    p1 = Proxy("socks5", "10.0.0.1", 1080)
    # p0 already used by 3 accounts -> next free is p1
    used = [p0, p0, p0]
    chosen = tdata_import.choose_pool_proxy(used, [p0, p1])
    assert (chosen.ip) == "10.0.0.1"


def test_choose_pool_proxy_all_full_raises():
    from modules.types.proxy import Proxy
    p0 = Proxy("socks5", "10.0.0.0", 1080)
    import pytest
    with pytest.raises(ValueError):
        tdata_import.choose_pool_proxy([p0, p0, p0], [p0])


def test_choose_pool_proxy_same_gateway_different_logins():
    from modules.types.proxy import Proxy
    # rotating/residential providers: one host:port, a distinct proxy per login
    p0 = Proxy("socks5", "gw.example.com", 7000, "user1", "pw")
    p1 = Proxy("socks5", "gw.example.com", 7000, "user2", "pw")
    chosen = tdata_import.choose_pool_proxy([p0, p0, p0], [p0, p1])
    assert chosen.user == "user2"


def test_choose_pool_proxy_blank_credentials_match_none():
    from modules.types.proxy import Proxy
    # sessions/add_session.py stores a no-auth proxy with "" credentials, the pool
    # parses it with None: the same proxy (as_telethon is identical), so it is full
    manual = Proxy("socks5", "1.2.3.4", 1080, "", "")
    with pytest.raises(ValueError):
        tdata_import.choose_pool_proxy([manual] * 3, [Proxy("socks5", "1.2.3.4", 1080)])


def test_convert_keeps_existing_jsession(tmp_path, monkeypatch):
    monkeypatch.setattr(tdata_import, "TDesktop", _FakeTDesktop)
    sessions = tmp_path / "s"
    sessions.mkdir()
    existing = sessions / "79990001122.jsession"
    existing.write_text('{"password": "old-pw"}')

    phone = asyncio.run(tdata_import.convert(str(tmp_path), None, None, str(sessions)))

    assert phone == "79990001122"
    assert json.loads(existing.read_text()) == {"password": "old-pw"}
