"""Account warmup (functions/warmup.py runs it, bot/routers/warmup.py shows it): its settings, the accounts'
progress, its content.

A project's settings: projects/<id>/warmup.json (load_settings: the defaults filled in). Each account's progress:
stats/warmup.json, {key: {"status", "day", "last", "done", "error", "warmed_at"}}, the key its Telegram user id (as
modules/profile_done.py has it: an account moved to another project keeps its progress), the session file's path
for a StringSession worker. An account is warmed once a day at most: its first run sets up its profile and
security (SETUP — a step that failed is tried again on its next day), every run reads channels, writes a note,
maybe starts a bot (ACTIVITY); after `days` runs with every setup step done it is warmed.

Its content: modules/content.py, the region's (the project's setting here, `region`).
"""
import datetime
import io
import os
import random
import re

from modules import content, json_file, profile_done, projects, restricted_workers
from modules.registry import title

STATE_PATH = os.path.join("stats", "warmup.json")
SETTINGS_NAME = "warmup.json"  # in the project's folder (projects.home)
# (key, title, what it does): the profile and the account's security, once (until done); the day's activity
PROFILE = (
    ("name", "👤 Имя", "случайное из names.txt"),
    ("username", "🔤 Username", "из имени: olga_petrova, opetrova92…"),
    ("photo", "🖼 Аватарки", "из photos/, каждая слегка уникализирована; прежние удаляются"),
    ("bio", "📝 Bio", "случайное из bios.txt"),
    ("music", "🎵 Музыка", "трек из music/ в профиле"),
    ("stickers", "🎨 Стикеры", "стикерпак из stickers.txt"),
)
SECURITY = (
    ("lastseen", "🙈 Скрыть визит", "«был(а) недавно» вместо точного времени"),
    ("2fa", "🔒 Пароль 2FA", "случайный, сохраняется в файле сессии; уже стоящий не меняется"),
    ("terminate", "📴 Чужие сессии", "выкидывает другие устройства; новой сессии Telegram даёт это через 24 ч"),
)
ACTIVITY = (
    ("channels", "📢 Каналы", "вступает в каналы из channels.txt, читает их и прежние"),
    ("bots", "🤖 Боты", "/start боту из bots.txt"),
    ("notes", "🗒 Заметки", "1–3 заметки в «Избранное», одна закрепляется"),
)
GROUPS = (("Оформление", PROFILE), ("Безопасность", SECURITY), ("Активность — каждый день", ACTIVITY))
STEPS = {key: title for key, title, _ in PROFILE + SECURITY + ACTIVITY}
SETUP = tuple(key for key, _, _ in PROFILE + SECURITY)  # once, until done
OFF, ON, KEEP = "off", "on", "keep"  # a step's mode; KEEP: its field only while it is empty
KEEPABLE = ("username", "photo", "bio", "music")  # the fields that may be empty (a name never is)

# a setting's values, its button cycling through them
CYCLES = {
    "region": list(content.REGIONS),
    "days": [1, 3, 5, 7, 14],
    "daily": [False, True],
    "avatars": ["1", "1-3"],
    "music_chance": [0, 30, 50, 70, 100],
    "stickers_chance": [0, 30, 50, 70, 100],
    "bots_chance": [0, 30, 50, 70, 100],
    # joined a day, at most as many as channels.txt holds; not "channels": the step's own key (STEPS)
    "channels_count": ["0-3", "1", "2", "3", "5", "3-7", "5-10"],
}
DEFAULTS = {
    "region": "ru", "days": 3, "threads": 1, "daily": False,
    "steps": {**dict.fromkeys(STEPS, ON), "username": KEEP},
    "avatars": "1", "music_chance": 30, "stickers_chance": 50, "bots_chance": 30, "channels_count": "0-3",
    "chat": 0,       # where the daily run reports: the chat that switched it on
    "auto_day": "",  # the day the daily run last started
}
# a step's random setting: its button next to the step's
RANDOM = {"photo": "avatars", "music": "music_chance", "stickers": "stickers_chance", "channels": "channels_count",
          "bots": "bots_chance"}

NEW, WARMING, WARMED, FAILED, RESTRICTED, DEAD = "new", "warming", "warmed", "failed", "restricted", "dead"
CATEGORIES = {NEW: "🆕 Новые", WARMING: "🌡 В прогреве", WARMED: "✅ Прогреты", FAILED: "❌ Сбои",
              RESTRICTED: "⛔ Ограничены", DEAD: "💀 Мёртвые"}


def today() -> str:
    return datetime.date.today().isoformat()


# --- a project's settings ---

def load_settings(home: str) -> dict:
    try:
        saved = json_file.load(os.path.join(home, SETTINGS_NAME), {})
    except (OSError, ValueError):  # a broken file (edited by hand): the defaults rather than a dead 🔥 screen
        saved = {}
    steps = {**DEFAULTS["steps"], **saved.get("steps", {})}
    steps = {key: ON if mode == KEEP and key not in KEEPABLE else mode for key, mode in steps.items()}
    return {**DEFAULTS, **saved, "steps": steps}


def region(home: str) -> str:
    """The project's region: its content's (modules/content.py), 👤 Профиль's lists too."""
    return load_settings(home)["region"]


def proxy_key(storage, path: str) -> str:
    """The address an account goes out from: its proxy's "ip:port", "" without one (the server's own)."""
    json_session = storage.jsessions_paths.get(path)
    proxy = json_session.account.proxy if json_session is not None else None
    return f"{proxy.ip}:{proxy.port}" if proxy else ""


def proxy_count(storage) -> int:
    """How many addresses the project's accounts go out from (proxy_key): the most warmed at once."""
    return len({proxy_key(storage, path) for path in storage.full_sessions})


def region_title(home: str) -> str:
    """🛠 Управление's 🌍 with the project's region on it: "🌍 Регион: СНГ (RU)"."""
    return f"{title('region')}: {content.REGIONS[region(home)].split(' ', 1)[1]}"


def save_settings(home: str, settings: dict):
    json_file.save(os.path.join(home, SETTINGS_NAME), settings)


def _next(values: list, current):
    return values[(values.index(current) + 1) % len(values)] if current in values else values[0]


def cycle(settings: dict, field: str) -> dict:
    """The setting's next value (CYCLES), or a step's next mode: off → on (→ only empty, a profile's) → off."""
    if field in STEPS:
        modes = [OFF, ON, KEEP] if field in KEEPABLE else [OFF, ON]
        settings["steps"][field] = _next(modes, settings["steps"][field])
    else:
        settings[field] = _next(CYCLES[field], settings[field])
    return settings


def count(setting: str) -> int:
    """How many of a "1-3" / "2" setting: a random one in the range."""
    low, _, high = setting.partition("-")
    return random.randint(int(low), int(high or low))


# --- the accounts' progress ---

def key(storage, path: str) -> str:
    if found := profile_done.worker_key(storage, path):
        return found
    user_id = (projects.read_account(path) or {}).get("user_id")  # a dead one's (inactive/): read offline
    return str(user_id) if user_id else path


def load_state() -> dict:
    try:
        return json_file.load(STATE_PATH, {})
    except (OSError, ValueError):  # a broken file (edited by hand): start over rather than break the screens
        return {}


def update(account: str, **fields):
    state = load_state()
    state.setdefault(account, {}).update(fields)
    json_file.save(STATE_PATH, state)


def forget(accounts: list):
    """Their progress dropped: warmed again from the first day (🔄), or the accounts deleted."""
    state = load_state()
    if any(account in state for account in accounts):
        json_file.save(STATE_PATH, {account: entry for account, entry in state.items() if account not in accounts})


def category(entry: dict | None) -> str:
    return NEW if entry is None else entry.get("status", WARMING)


def categories(storage) -> dict[str, list[str]]:
    """The project's account files by category (CATEGORIES), dead ones from its sessions/inactive/."""
    state, restricted = load_state(), set(restricted_workers.load())
    found = {name: [] for name in CATEGORIES}
    for path in projects.account_files(storage.directory):
        found[RESTRICTED if path in restricted else category(state.get(key(storage, path)))].append(path)
    found[DEAD] = projects.account_files(os.path.join(storage.directory, "inactive"))
    return found


def due(storage, paths: list[str]) -> list[str]:
    """The ones to warm today: not warmed yet, not today already, not permanently restricted."""
    state, restricted, now = load_state(), set(restricted_workers.load()), today()
    return [path for path in paths if path not in restricted
            and (entry := state.get(key(storage, path)) or {}).get("status") != WARMED and entry.get("last") != now]


def warmed_counts(storage) -> tuple[int, int]:
    """(warmed today, warmed in all) of the project's accounts."""
    state, now = load_state(), today()
    entries = [state.get(key(storage, path), {}) for path in projects.account_files(storage.directory)]
    warmed = [entry for entry in entries if entry.get("status") == WARMED]
    return sum(entry.get("warmed_at") == now for entry in warmed), len(warmed)


def summary(storage) -> str:
    """The project's warmup in a line — "✅ Прогрето 8 из 12 (сегодня 2) · ⛔ 1 · 💀 3" —, for 🔥 Прогрев, 🤖 Аккаунты
    and the project's card (📋 Список lists them); "" without accounts."""
    found = categories(storage)
    alive = sum(len(paths) for name, paths in found.items() if name != DEAD)
    if not alive and not found[DEAD]:
        return ""
    warmed_today, warmed = warmed_counts(storage)
    parts = [f"{CATEGORIES[WARMED].split(' ', 1)[0]} Прогрето {warmed} из {alive}"
             + (f" (сегодня {warmed_today})" if warmed_today else "")]
    parts += [f"{CATEGORIES[name].split(' ', 1)[0]} {len(found[name])}" for name in (RESTRICTED, DEAD) if found[name]]
    return " · ".join(parts)


# --- making a profile ---

_TRANSLIT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                     ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s",
                      "t", "u", "f", "kh", "ts", "ch", "sh", "shch", "", "y", "", "e", "yu", "ya"]))
_USERNAME = re.compile(r"[a-z][a-z0-9_]{4,31}")


def translit(text: str) -> str:
    """Latin letters and digits of a name, Cyrillic transliterated: "Ольга" → "olga"."""
    latin = "".join(_TRANSLIT.get(char, char) for char in text.lower())
    return re.sub(r"[^a-z0-9]", "", latin)


def username_candidates(first: str, last: str | None) -> list[str]:
    """Usernames as people make them of their name — olga_petrova, opetrova, olgapetrova92 —: the plain forms
    first, then with a year or a number; only valid ones."""
    first, last = translit(first), translit(last or "")
    first = first or "user"
    forms = [f"{first}{last}", f"{first}_{last}", f"{last}_{first}", f"{first[0]}{last}", f"{first}_{last[:1]}",
             f"{last}{first}"] if last else [first, f"{first}_{random.choice(['real', 'official', 'me', 'life'])}"]
    random.shuffle(forms)
    tails = [str(random.randint(80, 99)), f"0{random.randint(0, 9)}", str(random.randint(10, 999)),
             f"_{random.randint(1, 99)}", str(random.randint(1000, 9999))]
    candidates = forms + [f"{form}{tail}" for tail in tails for form in forms]
    return list(dict.fromkeys(name for name in candidates
                              if _USERNAME.fullmatch(name) and "__" not in name and not name.endswith("_")))


def unique_photo(path: str) -> io.BytesIO:
    """The photo a little different each time — cropped 1–4 % at the edges, re-encoded, no EXIF —, so the same
    picture on two accounts is not the same file: a JPEG in memory, for upload_file."""
    from PIL import Image  # only here: the rest of the module is read without Pillow

    with Image.open(path) as image:
        image = image.convert("RGB")
        width, height = image.size
        dx, dy = (int(side * random.uniform(0.01, 0.04)) for side in (width, height))
        image = image.crop((random.randint(0, dx), random.randint(0, dy),
                            width - random.randint(0, dx), height - random.randint(0, dy)))
        out = io.BytesIO()
        image.save(out, "JPEG", quality=random.randint(86, 95))
    out.name = "photo.jpg"  # upload_file takes the type from it
    out.seek(0)
    return out
