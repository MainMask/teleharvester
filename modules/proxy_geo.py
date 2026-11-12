"""The countries of a project's proxies — where their exit IPs are, as Telegram sees them — and its accounts moved onto
proxies of their numbers' countries: after 🌐 Прокси, and after accounts are added (bot/routers/accounts/proxies.py,
rebalance_now).

A proxy's country: ipinfo.io asked through the proxy itself (https://ipinfo.io/country → "RU"), so the service sees the
proxy's exit IP only — no account, no login. Kept in projects/<id>/proxies_geo.json by proxy (scheme://user@ip:port,
no password: a rotating provider's one host:port has many logins); a check that failed is not kept, it is tried again
RETRY_AFTER later.

The accounts: on the project's pool (proxies.txt) only — the permanently restricted ones on their own proxies stay
(functions/spamblock.rehome), a `.session` worker keeps no proxy —, each onto a proxy of its number's country
(modules/phone_countries.country_of), at most ACCOUNTS_PER_PROXY a proxy; one with no such proxy free stays where it
is and is told, so proxies of its country can be added. An account already on a proxy of its country never moves,
nor one on a proxy whose country could not be told: it may well be its own.
"""
import asyncio
import hashlib
import math
import os
import re
import ssl
import time
from collections import Counter

from python_socks import ProxyType
from python_socks.async_.asyncio import Proxy as SocksProxy

from modules import json_file, phone_countries, projects, restricted_workers, tdata_import
from modules.registry import title
from modules.types.proxy import ACCOUNTS_PER_PROXY, Proxy

GEO_NAME = "proxies_geo.json"  # in the project's folder
HOST, PATH = "ipinfo.io", "/country"
TIMEOUT = 15  # seconds for one proxy's check
AT_ONCE = 5  # proxies checked side by side
RETRY_AFTER = 6 * 3600  # seconds before a proxy whose check failed is checked again

_failed: dict[str, float] = {}  # proxy -> when its check last failed (time.monotonic): in memory, a restart retries
_TYPES = {"socks5": ProxyType.SOCKS5, "socks4": ProxyType.SOCKS4, "http": ProxyType.HTTP}
_CODE = re.compile(rb"(?m)^([A-Z]{2})\s*$")  # a body of its own line: plain, or a chunk of a chunked one


def _name(proxy: Proxy) -> str:
    """The proxy in the cache: its password as a short hash, never itself — a provider may pick the exit's country
    by the password (`pass_country-kz`), so two that differ by it alone are two proxies."""
    tail = f"#{hashlib.sha256(proxy.password.encode()).hexdigest()[:8]}" if proxy.password else ""
    return f"{proxy.proxy_type}://{proxy.user or ''}@{proxy.ip}:{proxy.port}{tail}"


async def _fetch(proxy: Proxy) -> bytes:
    """The service's raw HTTP answer, asked through `proxy`."""
    sock = await SocksProxy(_TYPES[proxy.proxy_type], proxy.ip, proxy.port, proxy.user or None,
                            proxy.password or None).connect(HOST, 443, timeout=TIMEOUT)
    try:
        reader, writer = await asyncio.open_connection(sock=sock, ssl=ssl.create_default_context(),
                                                       server_hostname=HOST)
    except BaseException:  # the TLS handshake failed: the proxy's socket is no stream's yet, closed here
        sock.close()
        raise
    try:
        writer.write(f"GET {PATH} HTTP/1.1\r\nHost: {HOST}\r\nUser-Agent: curl/8.0\r\nAccept: */*\r\n"
                     "Connection: close\r\n\r\n".encode())
        await writer.drain()
        return await reader.read()  # to its end (Connection: close): headers and body may come apart
    finally:
        writer.close()


def _country(response: bytes) -> str | None:
    """The country code of a 200 answer; None for any other."""
    head, _, body = response.partition(b"\r\n\r\n")
    if not re.match(rb"HTTP/1\.[01] 200", head):
        return None
    found = _CODE.search(body)
    return found.group(1).decode() if found else None


async def lookup(proxy: Proxy) -> str | None:
    """The proxy's exit country ("RU"); None if it could not be told (a dead proxy, the service refusing)."""
    try:
        return _country(await asyncio.wait_for(_fetch(proxy), TIMEOUT))
    except Exception:
        return None


def _path(home: str) -> str:
    return os.path.join(home, GEO_NAME)


def _known(home: str) -> dict:
    """The countries found so far; {} for a broken file (edited by hand): checked again rather than break a screen."""
    try:
        return json_file.load(_path(home), {})
    except (OSError, ValueError):
        return {}


def cached(home: str, proxy: Proxy | None) -> str | None:
    """The proxy's country as last found, without asking (a card); None for none, or no project's (`home` None:
    the personal accounts)."""
    return _known(home).get(_name(proxy)) if proxy is not None and home else None


def first_of(home: str, proxies: list[Proxy], country: str | None) -> list[Proxy]:
    """`proxies`, the ones of `country` (as last found) first: a new account's proxy (modules/login.role_proxy)."""
    if not country:
        return proxies
    known = _known(home)
    return sorted(proxies, key=lambda proxy: known.get(_name(proxy)) != country)


def wait_minutes(count: int) -> int:
    """At most how long `count` proxies take to check (AT_ONCE at a time, TIMEOUT each), in whole minutes."""
    return math.ceil(math.ceil(count / AT_ONCE) * TIMEOUT / 60)


async def countries(home: str, proxies: list[Proxy], before=None) -> dict:
    """{proxy.key(): its country or None} of `proxies`: the ones not known yet checked now (AT_ONCE at a time), but
    one that failed less than RETRY_AFTER ago (a dead proxy must not hold every login and import up).
    `before(count)`, awaited only when some are checked — how many —: to say why the wait."""
    known, now = _known(home), time.monotonic()
    missing = list({_name(proxy): proxy for proxy in proxies
                    if _name(proxy) not in known and now - _failed.get(_name(proxy), -RETRY_AFTER) >= RETRY_AFTER}
                   .values())
    if missing:
        if before is not None:
            await before(len(missing))
        semaphore = asyncio.Semaphore(AT_ONCE)

        async def one(proxy):
            async with semaphore:
                return await lookup(proxy)

        results = await asyncio.gather(*map(one, missing))
        _failed.update((_name(proxy), now) for proxy, code in zip(missing, results) if not code)
        found = {_name(proxy): code for proxy, code in zip(missing, results) if code}
        if found:
            known = {**_known(home), **found}
            json_file.save(_path(home), known)
    return {proxy.key(): known.get(_name(proxy)) for proxy in proxies}


async def ensure(home: str, before=None):
    """The countries of the project's pool known before a new account takes one of its proxies (role_proxy reads
    them), so its first login is from its own country's IP; a broken list is told where it is used. `before`: as
    countries'."""
    try:
        pool = tdata_import.load_proxies(projects.proxies_path(home))
    except ValueError:
        return
    await countries(home, pool, before)


def plan(accounts: dict, pool: list[Proxy], found: dict, busy=()) -> dict:
    """Where the accounts go: `accounts` {path: (its country or None, its proxy or None)}, the ones on the pool;
    `found` {proxy.key(): country or None} (countries). The ones on a proxy of another country, or on none (of a
    known country, not `busy`), give their places up, then each takes the least used proxy of its country with a
    place free;
    one with none goes back to its proxy (or, taken meanwhile, to any with a place) and is told among "unmatched".
    So two accounts on each other's country's proxies, both full, change places.
    Returns {"moves": {path: Proxy}, "unmatched": Counter(country), "busy": [paths]}."""
    keys = {proxy.key() for proxy in pool}
    load = Counter(proxy.key() for _, proxy in accounts.values() if proxy is not None and proxy.key() in keys)

    def misplaced(path):
        """Of a known country, on no proxy or on one known to be of another: one whose proxy's country could not
        be told may well be on its own — not moved for it."""
        country, proxy = accounts[path]
        return bool(country) and (proxy is None or found.get(proxy.key()) not in (None, country))

    waiting = [path for path in sorted(accounts) if misplaced(path)]
    # the ones with no proxy last: they take only places left free, never one given up by an account that, finding
    # no proxy of its country, must have it back
    movable = sorted((path for path in waiting if path not in busy), key=lambda path: accounts[path][1] is None)
    for path in movable:
        if (proxy := accounts[path][1]) is not None:
            load[proxy.key()] -= 1

    def least(candidates):
        return min(candidates, key=lambda proxy: load[proxy.key()], default=None)

    moves, unmatched = {}, Counter()
    for path in movable:
        country, old = accounts[path]
        target = least(p for p in pool if found.get(p.key()) == country and load[p.key()] < ACCOUNTS_PER_PROXY)
        if target is None:
            unmatched[country] += 1
            if old is None or load[old.key()] < ACCOUNTS_PER_PROXY:
                target = old
            else:  # its place taken by one of the country: any place free (as many as were given up)
                target = least(p for p in pool if load[p.key()] < ACCOUNTS_PER_PROXY) or old
        if target is not None:
            load[target.key()] += 1
        if (target and target.key()) != (old and old.key()):
            moves[path] = target
    return {"moves": moves, "unmatched": unmatched, "busy": [path for path in waiting if path in busy]}


def report(result: dict, unknown: int) -> str:
    """What rebalance did, in a few lines; `unknown`: the pool's proxies whose country could not be told."""
    if not (result["moves"] or result["unmatched"] or result["busy"] or unknown):
        return "🌍 Страны аккаунтов и прокси совпадают."
    lines = [f"🌍 Прокси по странам: пересажено аккаунтов — {len(result['moves'])}."]
    if result["unmatched"]:
        countries_ = ", ".join(f"{phone_countries.flag(code)} {code} — {count}"
                               for code, count in result["unmatched"].most_common())
        lines.append(f"⚠️ Нет свободного прокси своей страны: {countries_}. Эти аккаунты остались без прокси своей "
                     f"страны — добавьте прокси этих стран: «{title('proxy')}».")
    if result["busy"]:
        lines.append(f"⏳ Заняты сейчас: {len(result['busy'])} — пересадятся в следующий раз.")
    if unknown:
        lines.append(f"⚠️ Не удалось узнать страну прокси: {unknown} (не отвечает?) — аккаунты на них не трогаю, "
                     "проверю позже (через 6 ч).")
    return "\n".join(lines)


def _lists(home: str) -> tuple[list[Proxy], list[Proxy]]:
    """(the project's pool, its proxies for the restricted); ValueError for one edited badly."""
    return (tdata_import.load_proxies(projects.proxies_path(home)),
            tdata_import.load_proxies(projects.restricted_proxies_path(home)))


async def rebalance(storage, in_use=set) -> str:
    """The project's accounts onto proxies of their countries (see the module's doc), but the `in_use()` ones —
    asked after the countries are checked, right before the moves, as an account may be taken meanwhile. The
    report, or "" without a proxy list."""
    try:
        pool, _ = _lists(storage.home)
        found = await countries(storage.home, pool) if pool else {}
        # read again: a list sent while the countries were checked replaced this one (its new proxies' countries
        # unknown till the next check: no one moves onto them now)
        pool, spare = _lists(storage.home)
    except ValueError as err:  # a list edited by hand
        return f"⚠️ Страны прокси не сверены: {err}. Исправьте: «{title('proxy')}»."
    if not pool:
        return ""
    found = {proxy.key(): found.get(proxy.key()) for proxy in pool}
    # the accounts as they are now — another rebalance may have moved some while the countries were checked —, and
    # no await from here to the moves: none is taken or moved in between
    # the restricted ones go on their own proxies (rehome), never by country: one still on the pool, not moved there
    # yet, is counted as no country's — it takes its place, and moves not
    aside = set(restricted_workers.load()) if spare else set()
    keys = {proxy.key() for proxy in pool}
    accounts = {path: (None if path in aside else phone_countries.country_of(js.account.account.phone_number),
                       js.account.proxy)
                for path, js in storage.jsessions_paths.items()
                if (js.account.proxy is None and path not in aside)
                or (js.account.proxy is not None and js.account.proxy.key() in keys)}
    result = plan(accounts, pool, found, in_use())
    for path, proxy in result["moves"].items():
        storage.set_proxy(path, proxy)
    return report(result, sum(code is None for code in found.values()))


def summary(home: str, proxies: list[Proxy]) -> str:
    """The proxies by country as last found — "🇷🇺 5 · 🇰🇿 2 · ❔ 1" —, for 🌐 Прокси; "" with none known yet."""
    known = _known(home)
    counts = Counter(known.get(_name(proxy)) for proxy in proxies)
    if not any(code for code in counts):
        return ""
    return " · ".join(f"{phone_countries.flag(code) if code else '❔'} {count}"
                      for code, count in sorted(counts.items(), key=lambda row: (row[0] is None, -row[1])))
