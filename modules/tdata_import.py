"""Convert Telegram Desktop `tdata` folders into `.jsession` files.

Each account lives in `tdata_import/<name>/tdata`, optionally next to a
`Пароль 2фа*.txt` file holding its two-step password. Proxies (one per
account) come from the project's `proxies.txt`. Converted accounts are written to
the project's `sessions/<phone>.jsession` and the source folder is marked so a later run
does not reconnect it again.
"""

import asyncio
import io
import json
import os
import re
import zipfile

from opentele.api import UseCurrentSession
from opentele.td import TDesktop
from telethon.sessions import StringSession
from telethon.tl.functions.account import UpdatePersonalChannelRequest
from telethon.tl.types import InputChannelEmpty

from modules import atomic_file, projects, scraper_creds
from modules.console import console
from modules.types.account_settings import AccountSettings
from modules.types.proxy import ACCOUNTS_PER_PROXY, Proxy, parse_proxies

def save_proxies(text: str, path: str) -> None:
    """Make `text` (one proxy per line) a project's pool (projects.proxies_path): its phone logins and
    tdata imports take theirs from it."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    # owner-only, as a session's key: its lines hold the proxies' logins and passwords
    atomic_file.write(path, "\n".join(line.strip() for line in text.splitlines() if line.strip()) + "\n",
                      mode=0o600)


def load_proxies(path: str) -> list[Proxy]:
    """Read proxies (one per line, `scheme://[user:pass@]ip:port`) from `path`.

    A missing file means "no proxies" (empty list); blank lines and `#`
    comments are skipped.
    """
    if not os.path.exists(path):
        return []

    with open(path, encoding="utf-8-sig") as fileobj:
        return parse_proxies(fileobj.read())


MAX_UNZIPPED = 100 * 1024 * 1024  # a tdata folder is small: a 10 MB archive mustn't fill the disk


def unpack(archive_bytes: bytes, into: str) -> str | None:
    """A ZIP of a tdata folder unpacked into the folder `into`: the tdata folder in it, or None.
    ValueError for one that would unpack to more than MAX_UNZIPPED."""
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        if sum(info.file_size for info in archive.infolist()) > MAX_UNZIPPED:
            raise ValueError("Архив слишком велик в распакованном виде для папки tdata")
        archive.extractall(into)
    return find_tdata_dir(into)


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

    counts: dict = {}
    for proxy in used:
        if proxy is not None:
            counts[proxy.key()] = counts.get(proxy.key(), 0) + 1

    for proxy in available:
        if counts.get(proxy.key(), 0) < ACCOUNTS_PER_PROXY:
            return proxy

    raise ValueError(
        f"все прокси заняты (по {ACCOUNTS_PER_PROXY} аккаунта) — добавьте прокси в пул"
    )


# a .txt next to a tdata holding its 2FA password: named so (Пароль 2фа.txt, 2fa.txt, twoFA.txt, password.txt…),
# or the only .txt there; inside, a line "<label>: <password>" (or "="), or the password alone
PASSWORD_NAME = re.compile(r"2fa|2фа|two_?fa|pass|пароль", re.IGNORECASE)


def parse_2fa(text: str) -> str | None:
    """The password in a 2FA .txt: of its line labelled so (2fa: …, password = …), else of its only line —
    what follows its first ":" if it has one (the `<label>: <password>` of tdata_import/), else the line."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        labelled = re.match(r"([^:=]*)[:=]\s*(\S.*)", line)
        if labelled and PASSWORD_NAME.search(labelled.group(1)):
            return labelled.group(2).strip()
    if len(lines) == 1:
        _, colon, value = lines[0].partition(":")
        return value.strip() if colon and value.strip() else lines[0]
    return None


def _password_files(names: list[str]) -> list[str]:
    """The .txt of `names` that hold the 2FA password: the ones named so, else the only .txt."""
    texts = [name for name in names if name.lower().endswith(".txt")]
    named = [name for name in texts if PASSWORD_NAME.search(os.path.basename(name))]
    return named or (texts if len(texts) == 1 else [])


def read_2fa_password(worker_dir: str) -> str | None:
    """The two-step password from a .txt in `worker_dir` (_password_files, parse_2fa); None without one."""
    for name in _password_files(sorted(os.listdir(worker_dir))):
        with open(os.path.join(worker_dir, name), encoding="utf-8-sig", errors="replace") as fileobj:
            if password := parse_2fa(fileobj.read()):
                return password
    return None


def zip_2fa_password(archive_bytes: bytes) -> str | None:
    """The two-step password from a .txt in a tdata's ZIP (as read_2fa_password, anywhere in it)."""
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        for name in _password_files(sorted(info.filename for info in archive.infolist() if not info.is_dir())):
            if password := parse_2fa(archive.read(name).decode("utf-8-sig", "replace")):
                return password
    return None


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

    # the .txt read_2fa_password reads, its old password replaced (its label and other lines kept); else a new one
    matches = _password_files(sorted(os.listdir(worker_dir)))
    path = os.path.join(worker_dir, matches[0] if matches else "Пароль 2фа.txt")
    text = ""
    if matches:
        with open(path, encoding="utf-8-sig", errors="replace") as fileobj:
            text = fileobj.read()
    if old := parse_2fa(text):
        head, _, tail = text.rpartition(old)
        text = head + password + tail
    else:
        text = f"2фа пароль: {password}\n"
    atomic_file.write(path, text, mode=0o600)


def remember_password(storage, session, password: str):
    """A worker's new 2FA password, kept where it is read back: its .jsession (storage.remember_password) and the
    .txt next to its tdata (write_2fa_password), for a re-import."""
    storage.remember_password(session, password)
    if (json_session := storage.jsessions_paths.get(storage.get_session_path(session))) is not None:
        write_2fa_password(json_session.account.account.phone_number, password)


def _personal_user_ids() -> set[int]:
    """The user ids of the personal accounts (personal_sessions/): never workers."""
    return {account["user_id"] for path in projects.account_files(scraper_creds.PERSONAL_DIR)
            if (account := projects.read_account(path)) and "user_id" in account}


def _worker_phone(user_id: int, sessions_dir: str) -> str | None:
    """The phone of the worker already in sessions_dir with this user id (read offline), or None."""
    return next((account.get("phone_number") for path in projects.account_files(sessions_dir)
                 if (account := projects.read_account(path)) and account.get("user_id") == user_id), None)


def existing_worker(user_id: int, sessions_dir: str) -> str | None:
    """Before an account is imported (read offline: its key must not show up from a proxy needlessly): the phone
    of the project's worker with this id, if it is one; ValueError for a personal account or another place's
    worker — an account is in one place only (the .session import too, modules/session_import.py)."""
    if user_id in _personal_user_ids():
        raise ValueError(f"это личный аккаунт из {scraper_creds.PERSONAL_DIR}/ — воркером он не станет")
    if (phone := _worker_phone(user_id, sessions_dir)) is not None:
        return phone
    if (elsewhere := projects.find_account(user_id=user_id)) is not None:
        raise ValueError(f"этот аккаунт {projects.taken(elsewhere)}")
    return None


async def convert(tdata_dir: str, proxy: Proxy | None, password: str | None,
                  sessions_dir: str) -> str | None:
    """Convert one `tdata` folder to `<sessions_dir>/<phone>.jsession`.

    Returns the phone number on success, or None if the session is not
    authorized. The device model, app/system version, system language and API
    credentials opentele reconnects with are stored; `lang_pack` and `lang_code`
    are not reproduced on later loads (Telethon takes no `lang_pack`, and
    SessionsStorage reuses `system_lang_code` as `lang_code`). An account already in `sessions_dir` is
    found offline and left as is (its phone is returned): it isn't connected from the pool's proxy,
    and its file keeps its stored 2FA password and proxy. One that is a worker elsewhere (another
    project's, a free one): ValueError — an account is in one place only.
    """
    tdesktop = TDesktop(tdata_dir)
    if (phone := existing_worker(tdesktop.mainAccount.UserId, sessions_dir)) is not None:
        return phone

    client = await tdesktop.ToTelethon(
        session=StringSession(),
        flag=UseCurrentSession,
        proxy=proxy.as_telethon() if proxy else None,
    )
    phone, _ = await save_worker(client, proxy, password, client._init_request.lang_pack, sessions_dir)
    return phone


# connect() and the authorization check of an account being imported: Telethon retries a dead proxy for a minute
# or more, while the import holds the project's job slot
CONNECT_TIMEOUT = 40


async def save_worker(client, proxy: Proxy | None, password: str | None, lang_pack: str,
                      sessions_dir: str) -> tuple[str | None, bool]:
    """An imported account's fresh client (from tdata, or a .session: modules/session_import.py) into
    `<sessions_dir>/<phone>.jsession`, without the personal channel pinned to its profile: (its phone, True if it is
    new). (None, False) for a session no longer authorized; an account already the project's: its phone, its file
    left as is; a personal account or another place's worker: ValueError (existing_worker)."""
    async def account():
        await client.connect()
        return await client.get_me() if await client.is_user_authorized() else None

    try:
        try:
            me = await asyncio.wait_for(account(), CONNECT_TIMEOUT)
        except TimeoutError:  # its own text: a bare timeout's is empty, and the import's report shows it
            raise TimeoutError(f"Telegram не ответил за {CONNECT_TIMEOUT} с (мёртвый прокси или сеть)") from None
        if me is None:
            return None, False
        if (phone := existing_worker(me.id, sessions_dir)) is not None:
            return phone, False
        try:  # new workers start without a personal channel pinned to the profile
            await client(UpdatePersonalChannelRequest(InputChannelEmpty()))
        except Exception as err:
            console.print(f"[bold yellow]ВНИМАНИЕ:[/] не удалось убрать канал из профиля +{me.phone}: {err}")
        account_settings = AccountSettings.from_client(client, me, proxy, password, lang_pack)
    finally:
        await client.disconnect()

    os.makedirs(sessions_dir, exist_ok=True)
    out_path = os.path.join(sessions_dir, f"{me.phone}.jsession")
    if os.path.exists(out_path):  # one not read as a worker (broken?): kept as it is, not overwritten
        return me.phone, False
    account_settings.save(out_path)
    return me.phone, True


IMPORT_DIR = "tdata_import"  # the tdata folders to import: <name>/tdata


async def import_all(workers_dir: str, sessions_dir: str, proxies_path: str) -> int:
    """Convert every not-yet-imported `tdata` folder under `workers_dir`.

    A worker is skipped once a `.imported` marker sits in its folder; one that turns out to be
    a worker already gets the marker without being counted or taking a proxy. Each proxy
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

    # an account already a worker (read offline, as convert() does) is no new import and takes no
    # proxy: marked as imported, its file left as is
    new = []
    for worker_dir in pending:
        try:
            phone = _worker_phone(TDesktop(os.path.join(worker_dir, "tdata")).mainAccount.UserId, sessions_dir)
        except Exception:  # an unreadable tdata: convert() reports it
            phone = None
        if phone is None:
            new.append(worker_dir)
            continue
        open(os.path.join(worker_dir, ".imported"), "w").close()
        console.print(f"[bold white]Уже воркер:[/] {worker_dir} — {sessions_dir}/{phone}.jsession не изменён")
    pending = new
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
                used.append(Proxy(**proxy) if proxy else None)
            # a broken file is skipped by SessionsStorage too
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                continue

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
        console.print(f"[bold green]Импортирован[/] {worker_dir} -> {sessions_dir}/{phone}.jsession")
        imported += 1

    return imported
