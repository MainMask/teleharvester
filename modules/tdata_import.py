"""Convert Telegram Desktop `tdata` folders into `.jsession` files.

Each account lives in `tdata_import/<name>/tdata`, optionally next to a
`Пароль 2фа*.txt` file holding its two-step password. Proxies (one per
account) come from `assets/proxies.txt`. Converted accounts are written to
`sessions/<phone>.jsession` and the source folder is marked so a later run
does not reconnect it again.
"""

import glob
import json
import os

from opentele.api import UseCurrentSession
from opentele.td import TDesktop
from telethon.sessions import StringSession
from telethon.tl.functions.account import UpdatePersonalChannelRequest
from telethon.tl.types import InputChannelEmpty

from modules import scraper_creds
from modules.console import console
from modules.types.account_settings import AccountSettings
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


def write_2fa_password(phone: str, password: str, workers_dir: str = "tdata_import") -> None:
    """Store a changed 2FA password back next to the account's tdata, so a later
    re-import authorizes with the current one. Keeps the file's `<label>:` part;
    no-op when the account has no folder in `workers_dir`."""
    for name in (str(phone), f"+{phone}"):
        worker_dir = os.path.join(workers_dir, name)
        if os.path.isdir(worker_dir):
            break
    else:
        return

    matches = glob.glob(os.path.join(worker_dir, "Пароль 2фа*.txt"))
    path = matches[0] if matches else os.path.join(worker_dir, "Пароль 2фа.txt")
    label = "2фа пароль"
    if matches:
        with open(path, encoding="utf-8") as fileobj:
            label = fileobj.read().strip().partition(":")[0].strip() or label

    tmp_path = path + ".tmp"
    with os.fdopen(os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as fileobj:
        fileobj.write(f"{label}: {password}\n")
        fileobj.flush()
        os.fsync(fileobj.fileno())  # on disk before the rename: a power cut must not leave it empty
    os.replace(tmp_path, path)


def _personal_user_ids() -> set[int]:
    """The user ids of the personal accounts (personal_sessions/): never workers."""
    ids = set()
    directory = scraper_creds.PERSONAL_DIR
    if os.path.isdir(directory):
        for name in os.listdir(directory):
            if not name.endswith(".jsession"):
                continue
            try:
                with open(os.path.join(directory, name), encoding="utf-8") as fileobj:
                    ids.add(json.load(fileobj)["account"]["user_id"])
            except (OSError, ValueError, KeyError, TypeError):  # a broken file is skipped by SessionsStorage too
                continue
    return ids


def _worker_phone(user_id: int, sessions_dir: str) -> str | None:
    """The phone of the worker already in sessions_dir with this user id (read offline), or None."""
    if not os.path.isdir(sessions_dir):
        return None
    for name in os.listdir(sessions_dir):
        if not name.endswith(".jsession"):
            continue
        try:
            with open(os.path.join(sessions_dir, name), encoding="utf-8") as fileobj:
                account = json.load(fileobj)["account"]
            if account["user_id"] == user_id:
                return account["phone_number"]
        except (OSError, ValueError, KeyError, TypeError):  # a broken file is skipped by SessionsStorage too
            continue
    return None


async def convert(tdata_dir: str, proxy: Proxy | None, password: str | None,
                  sessions_dir: str = "sessions") -> str | None:
    """Convert one `tdata` folder to `sessions/<phone>.jsession`.

    Returns the phone number on success, or None if the session is not
    authorized. The device model, app/system version, system language and API
    credentials opentele reconnects with are stored; `lang_pack` and `lang_code`
    are not reproduced on later loads (Telethon takes no `lang_pack`, and
    SessionsStorage reuses `system_lang_code` as `lang_code`). An account already in `sessions_dir` is
    found offline and left as is (its phone is returned): it isn't connected from the pool's proxy,
    and its file keeps its stored 2FA password and proxy.
    """
    tdesktop = TDesktop(tdata_dir)
    # read offline, before connecting: its key must not show up from a worker's proxy
    if tdesktop.mainAccount.UserId in _personal_user_ids():
        raise ValueError(f"это личный аккаунт из {scraper_creds.PERSONAL_DIR}/ — воркером он не станет")
    # already a worker: its key must not show up from another proxy either
    if (phone := _worker_phone(tdesktop.mainAccount.UserId, sessions_dir)) is not None:
        return phone

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

        try:  # new workers start without a personal channel pinned to the profile
            await client(UpdatePersonalChannelRequest(InputChannelEmpty()))
        except Exception as err:
            console.print(f"[bold yellow]ВНИМАНИЕ:[/] не удалось убрать канал из профиля +{me.phone}: {err}")

        account_settings = AccountSettings.from_client(client, me, proxy, password, client._init_request.lang_pack)
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
            f"не хватает прокси для {len(pending)} новых аккаунтов: {err} ({proxies_path})"
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
            console.print(f"[bold yellow]ВНИМАНИЕ:[/] не удалось импортировать {worker_dir}: {err}")
            continue

        if phone is None:
            console.print(f"[bold yellow]ВНИМАНИЕ:[/] {worker_dir} не авторизован, пропущен")
            continue

        open(os.path.join(worker_dir, ".imported"), "w").close()
        console.print(f"[bold green]Импортирован[/] {worker_dir} -> sessions/{phone}.jsession")
        imported += 1

    return imported
