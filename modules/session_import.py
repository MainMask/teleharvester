"""Workers from Telethon `.session` files (SQLite), as sellers give them: often with a `.json` of the same name
holding the app and device the account was used with and its 2FA password. Read by the bot (➕ Добавить, files
or a ZIP) and sessions/add_session.py (the files in session_import/); written back the same way by export
(🤖 Аккаунты' 📦).

Each becomes the project's `sessions/<phone>.jsession` on one of the project's proxies (as a tdata import,
modules/tdata_import.py), presenting the app and device of its `.json` (a random official one without it). An
account already the project's is left as is; one that is a worker elsewhere or a personal account is refused.
A file imported (or found already imported) goes to `imported/` next to it, so a second run doesn't connect it
again.
"""
import json
import os
import random
import shutil
import tempfile

from telethon import TelegramClient
from telethon.sessions import SQLiteSession, StringSession

from modules import json_file, login, tdata_import
from modules.types.account_settings import AccountSettings

IMPORT_DIR = "session_import"  # sessions/add_session.py's: the .session (+ .json) files to import
DONE_DIR = "imported"


def collect(root: str) -> list[tuple[str, dict]]:
    """[(a .session file, its .json's data or {})] under `root`, imported ones (DONE_DIR) left out."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name != DONE_DIR)
        for name in sorted(filenames):
            if not name.endswith(".session"):
                continue
            meta_path = os.path.join(dirpath, name[:-len(".session")] + ".json")
            try:
                with open(meta_path, encoding="utf-8-sig") as fileobj:
                    meta = json.load(fileobj)
            except (OSError, ValueError):  # none, or not readable: a random app and device
                meta = {}
            found.append((os.path.join(dirpath, name), meta if isinstance(meta, dict) else {}))
    return found


def read_key(path: str) -> str:
    """The session's key and data center as a StringSession; read from a copy (opening one writes to it)."""
    with tempfile.TemporaryDirectory() as tmp:
        copy = os.path.join(tmp, "account.session")
        shutil.copyfile(path, copy)
        session = SQLiteSession(copy)
        try:
            if session.auth_key is None or not session.auth_key.key or not session.dc_id:
                raise ValueError("в файле нет ключа авторизации")
            return StringSession.save(session)
        finally:
            session.close()


async def convert(path: str, meta: dict, proxy, sessions_dir: str) -> tuple[str | None, bool]:
    """One .session to `<sessions_dir>/<phone>.jsession`: (its phone, True if it is new); (None, False) for a
    session no longer authorized."""
    key = read_key(path)
    if user_id := meta.get("user_id") or meta.get("id"):  # known offline: checked before connecting
        if (phone := tdata_import.existing_worker(int(user_id), sessions_dir)) is not None:
            return phone, False

    generator = random.choice(login.GENERATORS)
    lang = meta.get("system_lang_pack") or meta.get("system_lang_code") or generator.system_lang_code()
    client = TelegramClient(
        StringSession(key),
        meta.get("app_id") or generator.api_id,
        meta.get("app_hash") or generator.api_hash,
        device_model=meta.get("device") or meta.get("device_model") or generator.device(),
        app_version=meta.get("app_version") or generator.app_version(),
        system_version=meta.get("sdk") or meta.get("system_version") or generator.sdk(),
        lang_code=lang,  # as SessionsStorage.build_jsession_client loads it back
        system_lang_code=lang,
        proxy=proxy.as_telethon() if proxy else None,
    )
    password = meta.get("twoFA") or meta.get("2fa") or meta.get("password")
    return await tdata_import.save_worker(client, proxy, password, meta.get("lang_pack") or generator.lang_pack,
                                          sessions_dir)


def export(path: str, into: str) -> list[str]:
    """A worker's file as sellers give them, into the folder `into`: <name>.session (Telethon's SQLite) and, for a
    .jsession, <name>.json of its app, device, account and 2FA password — what convert reads back. The files
    written. ValueError / KeyError for a .jsession that can't be read."""
    base = os.path.join(into, os.path.splitext(os.path.basename(path))[0])
    if path.endswith(".session"):  # a worker's is a StringSession (SessionsStorage): its key, nothing else known
        with open(path, encoding="utf-8") as fileobj:
            key, settings = StringSession(fileobj.read().strip()), None
    else:
        settings = AccountSettings.from_dict(json_file.load(path, {}))
        key = StringSession(settings.auth_key)
    if key.auth_key is None:  # an empty file: no session to give
        raise ValueError("в файле нет ключа авторизации")
    session = SQLiteSession(base)  # base + ".session"
    try:
        session.set_dc(key.dc_id, key.server_address, key.port)
        session.auth_key = key.auth_key
        session.save()
    finally:
        session.close()
    if settings is None:
        return [base + ".session"]
    app, account = settings.application, settings.account
    meta = {"app_id": app.api_id, "app_hash": app.api_hash, "device": app.device_name, "app_version": app.app_version,
            "sdk": app.sdk, "lang_pack": app.lang_pack, "system_lang_pack": app.system_lang_code,
            "user_id": account.user_id, "phone": account.phone_number, "first_name": account.first_name,
            "last_name": account.last_name, "username": account.username, "twoFA": settings.password}
    json_file.save(base + ".json", meta)
    return [base + ".session", base + ".json"]


def _done(path: str):
    """The file (and its .json) into imported/ next to it."""
    done = os.path.join(os.path.dirname(path), DONE_DIR)
    os.makedirs(done, exist_ok=True)
    for source in (path, path[:-len(".session")] + ".json"):
        if os.path.exists(source):
            os.replace(source, os.path.join(done, os.path.basename(source)))


async def import_all(root: str, storage) -> tuple[list[str], list[tuple[str, str]]]:
    """Every .session under `root` into the project of `storage` (its live pool too): (the new .jsession files,
    [(file, why)] of the rest)."""
    added, failed = [], []
    for path, meta in collect(root):
        name = os.path.basename(path)
        try:  # a pool proxy, counting the accounts added just before (role_proxy reads the live pool)
            phone, new = await convert(path, meta, login.role_proxy("worker", storage), storage.directory)
        except Exception as err:
            failed.append((name, str(err)))
            continue
        if phone is None:
            failed.append((name, "сессия не авторизована"))
            continue
        if not new and not storage.is_phone_exists(phone):  # its file is there, but no worker is read from it:
            # left where it is, to be imported again once that file is gone
            failed.append((name, f"в проекте уже есть файл +{phone}, но бот его не читает — удалите его и "
                                 "импортируйте снова"))
            continue
        _done(path)
        if not new:
            failed.append((name, f"+{phone} уже воркер этого проекта — файл не изменён"))
            continue
        new_path = os.path.join(storage.directory, f"{phone}.jsession")
        storage.add_jsession(new_path)
        added.append(new_path)
    return added, failed
