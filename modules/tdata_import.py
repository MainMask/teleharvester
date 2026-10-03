"""Convert Telegram Desktop `tdata` folders into `.jsession` files.

Each account lives in `tdata_import/<name>/tdata`, optionally next to a
`Пароль 2фа*.txt` file holding its two-step password. Proxies (one per
account) come from `assets/proxies.txt`. Converted accounts are written to
`sessions/<phone>.jsession` and the source folder is marked so a later run
does not reconnect it again.
"""

import datetime
import glob
import json
import os

from opentele.api import UseCurrentSession
from opentele.td import TDesktop
from telethon.sessions import StringSession

from modules.console import console
from modules.types.account import Account
from modules.types.account_settings import AccountSettings
from modules.types.application import Application
from modules.types.proxy import ACCOUNTS_PER_PROXY, Proxy, parse_proxies

PROXIES_FILE = "assets/proxies.txt"  # the proxy pool, one `scheme://[user:pass@]ip:port` per line


def load_proxies(path: str = PROXIES_FILE) -> list[Proxy]:
    """Read proxies (one per line, `scheme://[user:pass@]ip:port`) from `path`.

    A missing file means "no proxies" (empty list); blank lines and `#`
    comments are skipped.
    """
    if not os.path.exists(path):
        return []

    with open(path, encoding="utf-8") as fileobj:
        return parse_proxies(fileobj.read())


def find_tdata_dir(root: str) -> str | None:
    """Locate the tdata folder inside an extracted archive tree.

    The tdata directory is the one holding the `key_datas` file (a reliable marker,
    regardless of the folder's name); returns its path, or None if not found.
    """
    for dirpath, _dirnames, filenames in os.walk(root):
        if "key_datas" in filenames:
            return dirpath
    return None


def choose_pool_proxy(used: list[Proxy | None], available: list[Proxy]) -> Proxy | None:
    """Pick a proxy for one new account, keeping at most ACCOUNTS_PER_PROXY per proxy.

    Counts how many existing accounts already use each proxy and returns the first
    `available` proxy with spare capacity. Empty pool -> None; all proxies full ->
    ValueError.
    """
    if not available:
        return None

    def key(proxy: Proxy):
        # credentials too: rotating providers expose many proxies on one host:port.
        # "" and None are the same no-auth proxy (as_telethon treats them alike)
        return (proxy.proxy_type, proxy.ip, proxy.port, proxy.user or None, proxy.password or None)

    counts: dict = {}
    for proxy in used:
        if proxy is not None:
            counts[key(proxy)] = counts.get(key(proxy), 0) + 1

    for proxy in available:
        if counts.get(key(proxy), 0) < ACCOUNTS_PER_PROXY:
            return proxy

    raise ValueError(
        f"все прокси заняты (по {ACCOUNTS_PER_PROXY} аккаунта) — добавьте прокси в пул"
    )


def read_2fa_password(worker_dir: str) -> str | None:
    """Return the two-step password from a `Пароль 2фа*.txt` file in `worker_dir`.

    The file is `<label>: <password>`; everything after the first colon is the
    password. Missing or malformed file -> None.
    """
    matches = glob.glob(os.path.join(worker_dir, "Пароль 2фа*.txt"))

    if not matches:
        return None

    with open(matches[0], encoding="utf-8") as fileobj:
        content = fileobj.read().strip()

    _, _, password = content.partition(":")
    password = password.strip()

    return password or None


async def convert(tdata_dir: str, proxy: Proxy | None, password: str | None,
                  sessions_dir: str = "sessions") -> str | None:
    """Convert one `tdata` folder to `sessions/<phone>.jsession`.

    Returns the phone number on success, or None if the session is not
    authorized. The device model, app/system version, system language and API
    credentials opentele reconnects with are stored; `lang_pack` and `lang_code`
    are not reproduced on later loads (Telethon takes no `lang_pack`, and
    SessionsStorage reuses `system_lang_code` as `lang_code`). An existing `.jsession` for that phone is left as is,
    keeping its stored 2FA password and proxy.
    """
    tdesktop = TDesktop(tdata_dir)

    client = await tdesktop.ToTelethon(
        session=StringSession(),
        flag=UseCurrentSession,
        proxy=proxy.as_telethon() if proxy else None,
    )

    await client.connect()

    try:
        if not await client.is_user_authorized():
            return None

        me = await client.get_me()
        init = client._init_request

        account_settings = AccountSettings(
            auth_key=client.session.save(),
            account=Account(
                first_name=me.first_name,
                last_name=me.last_name,
                user_id=me.id,
                added_at=datetime.datetime.now().timestamp(),
                phone_number=me.phone,
            ),
            application=Application(
                api_id=client.api_id,
                api_hash=client.api_hash,
                device_name=init.device_model,
                app_version=init.app_version,
                sdk=init.system_version,
                lang_pack=init.lang_pack,
                system_lang_code=init.system_lang_code,
            ),
            proxy=proxy,
            password=password,
        )
    finally:
        await client.disconnect()

    os.makedirs(sessions_dir, exist_ok=True)
    out_path = os.path.join(sessions_dir, f"{me.phone}.jsession")

    if not os.path.exists(out_path):
        account_settings.save(out_path)

    return me.phone


async def import_all(workers_dir: str = "tdata_import", sessions_dir: str = "sessions",
                     proxies_path: str = PROXIES_FILE) -> int:
    """Convert every not-yet-imported `tdata` folder under `workers_dir`.

    A worker is skipped once a `.imported` marker sits in its folder. Each proxy
    is shared by up to `ACCOUNTS_PER_PROXY` accounts, counting the accounts already
    in `sessions_dir`; if a proxies file exists but has too little spare capacity
    for the new accounts, nothing is converted (raises).
    """
    if not os.path.isdir(workers_dir):
        return 0

    pending = []

    for entry in sorted(os.listdir(workers_dir)):
        worker_dir = os.path.join(workers_dir, entry)

        if not os.path.isdir(worker_dir):
            continue
        if not os.path.isdir(os.path.join(worker_dir, "tdata")):
            continue
        if os.path.exists(os.path.join(worker_dir, ".imported")):
            continue

        pending.append(worker_dir)

    if not pending:
        return 0

    proxies = load_proxies(proxies_path)

    # proxies already taken by imported accounts, so a later run doesn't overload them
    used = []
    if os.path.isdir(sessions_dir):
        for name in os.listdir(sessions_dir):
            if not name.endswith(".jsession"):
                continue
            try:
                with open(os.path.join(sessions_dir, name)) as fileobj:
                    proxy = json.load(fileobj).get("proxy")
            except (OSError, ValueError):  # a broken file is skipped by SessionsStorage too
                continue
            used.append(Proxy(**proxy) if proxy else None)

    assigned = []
    try:
        for _ in pending:
            proxy = choose_pool_proxy(used, proxies)
            used.append(proxy)
            assigned.append(proxy)
    except ValueError as err:
        raise RuntimeError(
            f"not enough proxies for {len(pending)} new accounts: {err} ({proxies_path})"
        ) from err

    imported = 0

    for worker_dir, proxy in zip(pending, assigned):
        password = read_2fa_password(worker_dir)

        try:
            phone = await convert(
                os.path.join(worker_dir, "tdata"),
                proxy,
                password,
                sessions_dir,
            )
        except Exception as err:
            console.print(f"[bold yellow]WARNING:[/] failed to import {worker_dir}: {err}")
            continue

        if phone is None:
            console.print(f"[bold yellow]WARNING:[/] {worker_dir} is not authorized, skipped")
            continue

        open(os.path.join(worker_dir, ".imported"), "w").close()
        console.print(f"[bold green]Imported[/] {worker_dir} -> sessions/{phone}.jsession")
        imported += 1

    return imported
