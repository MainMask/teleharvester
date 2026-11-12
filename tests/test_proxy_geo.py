"""The proxies' countries and the accounts on proxies of their own (modules/proxy_geo.py,
modules/phone_countries.py)."""
import asyncio
import hashlib
import json
import os
from collections import Counter
from types import SimpleNamespace as ns

import pytest

from modules import login, phone_countries, projects, proxy_geo, restricted_workers
from modules.types.proxy import Proxy

import conftest

RU1, RU2, KZ1, US1 = (Proxy("socks5", f"10.0.0.{i}", 1080, "u", "secret") for i in range(1, 5))
FOUND = {RU1.key(): "RU", RU2.key(): "RU", KZ1.key(): "KZ", US1.key(): "US"}


def test_a_number_s_country_tells_the_countries_of_one_code_apart():
    assert [phone_countries.country_of(phone) for phone in ("79161234567", "+77011234567", "12025550123",
                                                            "14165550123")] == ["RU", "KZ", "US", "CA"]
    assert phone_countries.country_of("88812345678") is None  # Fragment's anonymous numbers: no country
    assert phone_countries.country_of(None) is None and phone_countries.country_of("abc") is None
    assert phone_countries.flag("RU") == "🇷🇺"


@pytest.mark.parametrize("response, code", [
    (b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nRU\n", "RU"),
    (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nKZ\n\r\n0\r\n\r\n", "KZ"),
    (b"HTTP/1.1 429 Too Many Requests\r\n\r\nRU\n", None),
    (b"HTTP/1.1 200 OK\r\n\r\n<html>blocked</html>", None),
    (b"", None),
])
def test_the_service_s_answer_is_its_country_code_or_nothing(response, code):
    assert proxy_geo._country(response) == code


def test_the_countries_are_checked_once_kept_without_passwords_and_a_failure_tried_again(monkeypatch):
    asked = []

    async def lookup(proxy):
        asked.append(proxy.ip)
        return {"10.0.0.1": "RU", "10.0.0.3": "KZ"}.get(proxy.ip)

    monkeypatch.setattr(proxy_geo, "lookup", lookup)
    os.makedirs(conftest.home(), exist_ok=True)
    found = asyncio.run(proxy_geo.countries(conftest.home(), [RU1, KZ1, US1]))
    assert found == {RU1.key(): "RU", KZ1.key(): "KZ", US1.key(): None}
    with open(os.path.join(conftest.home(), proxy_geo.GEO_NAME)) as fileobj:
        saved = json.load(fileobj)
    tag = "#" + hashlib.sha256(b"secret").hexdigest()[:8]  # the password as a short hash, never itself
    assert saved == {f"socks5://u@10.0.0.1:1080{tag}": "RU", f"socks5://u@10.0.0.3:1080{tag}": "KZ"}
    assert "secret" not in json.dumps(saved)
    asked.clear()
    asyncio.run(proxy_geo.countries(conftest.home(), [RU1, KZ1, US1]))
    assert asked == []  # the known ones not asked again, the failed one not for RETRY_AFTER: it holds nothing up
    later = proxy_geo.time.monotonic() + proxy_geo.RETRY_AFTER
    monkeypatch.setattr(proxy_geo.time, "monotonic", lambda: later)
    asyncio.run(proxy_geo.countries(conftest.home(), [RU1, KZ1, US1]))
    assert asked == ["10.0.0.4"]  # then it is
    assert proxy_geo.cached(conftest.home(), RU1) == "RU" and proxy_geo.cached(None, RU1) is None
    assert proxy_geo.summary(conftest.home(), [RU1, KZ1, US1]) == "🇷🇺 1 · 🇰🇿 1 · ❔ 1"  # the most first, then listed
    assert proxy_geo.first_of(conftest.home(), [US1, RU1, KZ1], "KZ") == [KZ1, US1, RU1]


def test_an_account_on_its_country_s_proxy_stays_one_on_another_moves():
    result = proxy_geo.plan({"a": ("RU", RU1), "b": ("RU", KZ1), "c": ("KZ", KZ1)}, [RU1, RU2, KZ1, US1], FOUND)
    assert result["moves"] == {"b": RU2}  # the least used of its country
    assert not result["unmatched"] and not result["busy"]


def test_two_accounts_on_each_other_s_full_proxies_change_places():
    accounts = {**{f"kz{i}": ("KZ", RU1) for i in range(3)}, **{f"ru{i}": ("RU", KZ1) for i in range(3)}}
    result = proxy_geo.plan(accounts, [RU1, KZ1], FOUND)
    assert result["moves"] == {**{f"kz{i}": KZ1 for i in range(3)}, **{f"ru{i}": RU1 for i in range(3)}}


def test_one_with_no_free_proxy_of_its_country_stays_and_is_told():
    accounts = {**{f"ru{i}": ("RU", RU1) for i in range(3)}, "late": ("RU", US1), "ua": ("UA", KZ1)}
    result = proxy_geo.plan(accounts, [RU1, KZ1, US1], FOUND)
    assert result["moves"] == {} and result["unmatched"] == Counter({"RU": 1, "UA": 1})


def test_a_busy_one_one_of_unknown_country_and_one_on_a_proxy_of_unknown_country_do_not_move():
    found = {**FOUND, US1.key(): None}  # its check failed: it may well be KZ
    accounts = {"busy": ("RU", KZ1), "nophone": (None, KZ1), "on_unknown": ("KZ", US1), "none": ("KZ", None)}
    result = proxy_geo.plan(accounts, [RU1, KZ1, US1], found, busy={"busy"})
    assert result["moves"] == {"none": KZ1} and result["busy"] == ["busy"]
    assert not result["unmatched"]  # nor told as having no proxy of its country


def test_a_proxy_s_country_is_read_when_its_headers_and_body_come_apart(monkeypatch):
    class _Reader:
        def __init__(self):
            self.chunks = [b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\n", b"RU\n"]

        async def read(self, n=-1):
            if n == -1:  # to the end
                data, self.chunks = b"".join(self.chunks), []
                return data
            return self.chunks.pop(0) if self.chunks else b""

    class _Writer:
        def write(self, data):
            pass

        async def drain(self):
            pass

        def close(self):
            pass

    class _Socks:
        def __init__(self, *args):
            pass

        async def connect(self, *args, **kwargs):
            return ns(close=lambda: None)

    async def open_connection(**kwargs):
        return _Reader(), _Writer()

    monkeypatch.setattr(proxy_geo, "SocksProxy", _Socks)
    monkeypatch.setattr(proxy_geo.asyncio, "open_connection", open_connection)
    assert proxy_geo._country(asyncio.run(proxy_geo._fetch(RU1))) == "RU"


def test_the_proxy_s_socket_is_closed_when_the_tls_handshake_fails(monkeypatch):
    closed = []

    class _Socks:
        def __init__(self, *args):
            pass

        async def connect(self, *args, **kwargs):
            return ns(close=lambda: closed.append(True))

    async def open_connection(**kwargs):
        raise ConnectionResetError("handshake")

    monkeypatch.setattr(proxy_geo, "SocksProxy", _Socks)
    monkeypatch.setattr(proxy_geo.asyncio, "open_connection", open_connection)
    with pytest.raises(ConnectionResetError):
        asyncio.run(proxy_geo._fetch(RU1))
    assert closed == [True]


def test_a_broken_countries_file_reads_as_empty():
    os.makedirs(conftest.home(), exist_ok=True)
    with open(os.path.join(conftest.home(), proxy_geo.GEO_NAME), "w") as fileobj:
        fileobj.write("{broken")
    assert proxy_geo.cached(conftest.home(), RU1) is None and proxy_geo.summary(conftest.home(), [RU1]) == ""
    assert proxy_geo.first_of(conftest.home(), [US1, RU1], "RU") == [US1, RU1]


class _Storage:
    def __init__(self, accounts):
        self.home = conftest.home()
        self.jsessions_paths = {path: ns(account=ns(account=ns(phone_number=phone), proxy=proxy))
                                for path, (phone, proxy) in accounts.items()}
        self.moved = {}

    def set_proxy(self, path, proxy):
        self.jsessions_paths[path].account.proxy = proxy
        self.moved[path] = proxy


def _lists(pool, spare=""):
    os.makedirs(conftest.home(), exist_ok=True)
    with open(projects.proxies_path(conftest.home()), "w") as fileobj:
        fileobj.write(pool)
    if spare:
        with open(projects.restricted_proxies_path(conftest.home()), "w") as fileobj:
            fileobj.write(spare)


def test_rebalance_moves_the_pool_s_accounts_but_the_restricted_on_theirs_and_tells_it(monkeypatch):
    ru, kz = Proxy("socks5", "1.1.1.1", 1080), Proxy("socks5", "2.2.2.2", 1080)
    spare = Proxy("socks5", "9.9.9.9", 1080)
    _lists("socks5://1.1.1.1:1080\nsocks5://2.2.2.2:1080\n", "socks5://9.9.9.9:1080\n")

    async def lookup(proxy):
        return {"1.1.1.1": "RU", "2.2.2.2": "KZ"}[proxy.ip]

    monkeypatch.setattr(proxy_geo, "lookup", lookup)
    storage = _Storage({"ru": ("79161234567", kz), "kz": ("77011234567", kz), "ua": ("380501234567", ru),
                        "held": ("79161234568", spare), "busy": ("77011234568", ru)})
    restricted_workers.save(["held"])
    text = asyncio.run(proxy_geo.rebalance(storage, lambda: {"busy"}))
    assert storage.moved == {"ru": ru}
    assert text == ("🌍 Прокси по странам: пересажено аккаунтов — 1.\n"
                    "⚠️ Нет свободного прокси своей страны: 🇺🇦 UA — 1. Эти аккаунты остались без прокси своей страны — "
                    "добавьте прокси этих стран: «🌐 Прокси».\n"
                    "⏳ Заняты сейчас: 1 — пересадятся в следующий раз.")
    assert asyncio.run(proxy_geo.rebalance(_Storage({"kz": ("77011234567", kz)}))) == \
        "🌍 Страны аккаунтов и прокси совпадают."


def test_rebalance_without_a_list_says_nothing_and_with_a_broken_one_says_so():
    assert asyncio.run(proxy_geo.rebalance(_Storage({}))) == ""
    _lists("not a proxy\n")
    assert asyncio.run(proxy_geo.rebalance(_Storage({}))).startswith("⚠️ Страны прокси не сверены")


def test_a_new_account_gets_a_proxy_of_its_number_s_country_first(monkeypatch):
    _lists("socks5://1.1.1.1:1080\nsocks5://2.2.2.2:1080\n")
    with open(os.path.join(conftest.home(), proxy_geo.GEO_NAME), "w") as fileobj:
        json.dump({"socks5://@1.1.1.1:1080": "RU", "socks5://@2.2.2.2:1080": "KZ"}, fileobj)
    workers = ns(home=conftest.home(), jsessions_paths={})
    assert login.role_proxy("worker", workers, "77011234567").ip == "2.2.2.2"
    assert login.role_proxy("worker", workers, "79161234567").ip == "1.1.1.1"
    assert login.role_proxy("worker", workers).ip == "1.1.1.1"  # the number unknown: the first free


def test_with_no_countries_known_yet_a_new_account_s_proxy_is_of_its_country_once_they_are_found(monkeypatch):
    _lists("socks5://1.1.1.1:1080\nsocks5://2.2.2.2:1080\n")

    async def lookup(proxy):
        return {"1.1.1.1": "US", "2.2.2.2": "RU"}[proxy.ip]

    monkeypatch.setattr(proxy_geo, "lookup", lookup)
    workers = ns(home=conftest.home(), jsessions_paths={})
    assert login.role_proxy("worker", workers, "79161234567").ip == "1.1.1.1"  # nothing known: the first free
    asyncio.run(proxy_geo.ensure(conftest.home()))  # what a login by phone and a .session import do first
    assert login.role_proxy("worker", workers, "79161234567").ip == "2.2.2.2"


def test_a_failed_rebalance_is_told_not_raised(monkeypatch):
    from bot.routers.accounts import proxies

    async def rebalance(storage, in_use):
        raise OSError("диск полон")

    monkeypatch.setattr(proxy_geo, "rebalance", rebalance)
    pool = ns(storage=ns(jsessions_paths={}), busy=lambda path: False, scraped=lambda path: False)
    assert asyncio.run(proxies.rebalance_now(pool)) == "⚠️ Пересадка по странам не удалась: диск полон"


def test_a_login_s_summary_shows_the_proxy_it_is_on_after_the_rebalance(monkeypatch):
    from bot.routers import login as login_router

    first, final = Proxy("socks5", "1.1.1.1", 1080), Proxy("socks5", "2.2.2.2", 1080)
    storage = ns(jsessions_paths={})

    def store(login_, me, role, workers, personal):
        storage.jsessions_paths["sessions/7.jsession"] = ns(account=ns(proxy=first))
        return "sessions/7.jsession"

    async def rebalance_now(pool):
        storage.jsessions_paths["sessions/7.jsession"].account.proxy = final
        return "🌍 Прокси по странам: пересажено аккаунтов — 1."

    class _State:
        async def get_data(self):
            return {"role": "worker", "project": 1}

        async def clear(self):
            pass

    answers = []
    monkeypatch.setattr(login_router.sign_in, "already_known", lambda *args: False)
    monkeypatch.setattr(login_router.sign_in, "store", store)
    monkeypatch.setattr(login_router, "rebalance_now", rebalance_now)
    message = ns(chat=ns(id=1), answer=lambda text, **kw: asyncio.sleep(0, answers.append(text)))
    login_ = ns(proxy=first, password=None, client=None)
    asyncio.run(login_router._finish(message, _State(), ns(storage=storage, count=lambda: 1), None, login_,
                                     ns(phone="79161234567")))
    assert "🌐 Прокси: socks5://2.2.2.2:1080" in answers[-1] and "пересажено аккаунтов — 1" in answers[-1]


def test_the_wait_for_the_countries_is_told_only_when_some_are_checked(monkeypatch):
    _lists("socks5://1.1.1.1:1080\n")

    async def lookup(proxy):
        return "RU"

    monkeypatch.setattr(proxy_geo, "lookup", lookup)
    told = []

    async def before(count):
        told.append(count)

    asyncio.run(proxy_geo.ensure(conftest.home(), before))
    asyncio.run(proxy_geo.ensure(conftest.home(), before))  # known now: no wait, nothing said
    assert told == [1]


def test_one_with_no_proxy_takes_no_place_one_finding_no_proxy_of_its_country_must_have_back():
    """RU proxy P full of KZ numbers, no KZ proxy, a RU number with no proxy: P keeps its three."""
    accounts = {"a1": ("RU", None), "k1": ("KZ", RU1), "k2": ("KZ", RU1), "k3": ("KZ", RU1)}
    result = proxy_geo.plan(accounts, [RU1], FOUND)
    assert result["moves"] == {} and result["unmatched"] == Counter({"KZ": 3, "RU": 1})


def test_a_restricted_one_still_on_a_pool_proxy_holds_its_place_and_stays(monkeypatch):
    ru = Proxy("socks5", "1.1.1.1", 1080)
    _lists("socks5://1.1.1.1:1080\n", "socks5://9.9.9.9:1080\n")

    async def lookup(proxy):
        return "RU"

    monkeypatch.setattr(proxy_geo, "lookup", lookup)
    storage = _Storage({"a": ("79161234567", ru), "b": ("79161234568", ru), "held": ("77011234567", ru),
                        "x": ("79161234569", None)})
    restricted_workers.save(["held"])  # restricted, not moved onto its own proxies yet
    asyncio.run(proxy_geo.rebalance(storage))
    assert storage.moved == {}  # P full with "held": x not put there as a fourth, "held" not moved by country


def test_proxies_differing_by_the_password_alone_are_two_countries(monkeypatch):
    kz, ru = (Proxy("socks5", "5.5.5.5", 1080, "u", f"pass_country-{code}") for code in ("kz", "ru"))

    async def lookup(proxy):
        return proxy.password[-2:].upper()

    monkeypatch.setattr(proxy_geo, "lookup", lookup)
    os.makedirs(conftest.home(), exist_ok=True)
    assert asyncio.run(proxy_geo.countries(conftest.home(), [kz, ru])) == {kz.key(): "KZ", ru.key(): "RU"}


def test_the_wait_is_told_in_minutes_it_may_really_take():
    assert proxy_geo.wait_minutes(1) == 1 and proxy_geo.wait_minutes(60) == 3  # 12 rounds of 15 s


def test_the_table_is_downloaded_whole_however_it_comes_in(monkeypatch):
    from bot.services import id_dates_update

    class _Content:
        async def iter_chunked(self, size):
            for chunk in (b'[{"id": "1", ', b'"date": "2020-01-01"}]'):
                yield chunk

    class _Response:
        content = _Content()

        def raise_for_status(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _Session:
        def __init__(self, **kwargs):
            pass

        def get(self, url):
            return _Response()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(id_dates_update.aiohttp, "ClientSession", _Session)
    assert json.loads(asyncio.run(id_dates_update.fetch())) == [{"id": "1", "date": "2020-01-01"}]
    monkeypatch.setattr(id_dates_update, "MAX_SIZE", 10)
    with pytest.raises(ValueError, match="слишком большой"):
        asyncio.run(id_dates_update.fetch())


def test_a_list_replaced_while_the_countries_are_checked_is_the_one_used(monkeypatch):
    """No account goes onto a proxy of the list just dropped: the lists are read again after the wait."""
    _lists("socks5://1.1.1.1:1080\n")

    async def lookup(proxy):
        _lists("socks5://2.2.2.2:1080\n")  # 🌐 Прокси meanwhile: a new list
        return "RU"

    monkeypatch.setattr(proxy_geo, "lookup", lookup)
    storage = _Storage({"x": ("79161234567", None)})
    text = asyncio.run(proxy_geo.rebalance(storage))
    assert storage.moved == {}  # not onto 1.1.1.1 (gone), nor onto 2.2.2.2 (its country not known yet)
    assert "Не удалось узнать страну прокси: 1" in text
