"""Projects: named sets of scrape sources, kept in stats/projects.json and shared by the bot
and the CLI. A project's name is the name its scrapes run under, so its files are the
<name>_posts / _reactors / _participants ones in the bases folder (scraped_files.project_files)
and an interrupted scrape of it is continued by that name (scraper.scrape.pending_resume).

All the work is a project's: its workers, proxies, mailing stats and contacts ledger live in its own
folder, projects/<id>/ (home), named by its id so a rename moves none of them. An account is in one
project only; one in none is free (FREE_DIR) and works nowhere until moved into one (move_account)."""
import html as html_text
import os
import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from datetime import date, datetime, timedelta, timezone

from modules import atomic_file, json_file, restricted_workers, scraped_files
from modules.storages.sessions_storage import SessionsStorage
from scraper.datafiles import read_attrs
from scraper.scrape import ChatInfo, chat_block, pending_resume

PATH = os.path.join("stats", "projects.json")
PROJECTS_DIR = "projects"  # projects/<id>/: a project's own folder (home)
FREE_DIR = "sessions"  # accounts in no project (the workers from before projects had their own)
# the shared mailing stats and contacts ledger from before projects had their own: each project there
# then got a copy (migrate), so none of them writes again to someone already written to
# ({its name in a project's folder: the shared file})
LEGACY_STATS = {"pm_mailing.json": os.path.join("stats", "pm_mailing.json"),
                "contacts.json": os.path.join("stats", "contacts.json")}
MAX_NAME = 40  # a project's name is a button label and a file name prefix
# the card must stay within a Telegram message (4096): the channels' text and the files listed
MAX_CHANNELS_TEXT = 1500
MAX_CARD_FILES = 20
MAX_CARD_CHANNELS = 20
MAX_CARD_TEXT = 3800  # the bot's card as sent (escaped HTML): its channels fill what the rest leaves
UNKNOWN_CHAT = "❔ название — при «▶️ Скрап проекта»"  # a channel no check has named yet
TOPUP_OVERLAP = timedelta(days=7)  # a top-up re-reads its last week: late comments and reactions under it


@dataclass
class Project:
    name: str
    # as typed: @name / t.me link / invite / numeric id (see parse_channels); none for a project whose
    # base comes from groups' members or a list only — its scrape waits for channels
    channels: list[str]
    # a chat (as typed) -> {"title", "id", "username"} as the last check on an account named it
    # (remember): its channels, and every other chat checked in its work (a list corrected for one
    # scrape, the groups of 👥, a verify's channel), so each is shown by name; one never checked is not here
    chats: dict[str, dict] = field(default_factory=dict)
    id: int = 0  # its folder's name (home): never changes, a rename keeps it; 0 until migrate() gives one

    def chat(self, raw: str) -> ChatInfo:
        """The chat as last checked (no problem kept: it is the checking account's), else as typed."""
        known = self.chats.get(raw, {})
        return ChatInfo(raw, known.get("title"), None, known.get("id"), known.get("username"))


def load() -> list[Project]:
    """The projects; none without the file (an install without projects works as before).
    ValueError, with the reason, if the file can't be read: it is never overwritten then."""
    try:
        data = json_file.load(PATH, [])
        return [Project(name=item["name"], channels=list(item["channels"]), chats=dict(item.get("chats", {})),
                        id=item.get("id", 0)) for item in data]
    except (OSError, ValueError, TypeError, KeyError) as err:
        raise ValueError(f"{PATH} не читается ({type(err).__name__}: {err}) — исправьте или удалите его; "
                         f"предыдущая версия: {PATH}.bak") from err


def _save(projects: list[Project]):
    """The previous file is copied to .bak first: every write can be undone once."""
    if os.path.exists(PATH):
        shutil.copy2(PATH, PATH + ".bak")
    json_file.save(PATH, [asdict(project) for project in projects])


def name_problem(name: str, projects: list[Project]) -> str | None:
    """Why `name` can't be a new project's name, or None: a scrape's name (it is one) that fits a
    button and isn't another project's."""
    if problem := scraped_files.name_problem(name):
        return problem
    if len(name) > MAX_NAME:
        return f"имя длиннее {MAX_NAME} символов"
    if any(project.name.casefold() == name.casefold() for project in projects):
        return "проект с таким именем уже есть"
    return None


def add(name: str, channels: list[str]) -> Project:
    """A new project, with no channels too; ValueError with the reason on a bad name."""
    projects = load()
    name = name.strip()
    if problem := name_problem(name, projects):
        raise ValueError(problem)
    project = Project(name, list(channels), id=max((project.id for project in projects), default=0) + 1)
    _save([*projects, project])
    return project


def set_channels(name: str, channels: list[str]) -> Project:
    """Replace the project's channels (none: no scrape until it has some); ValueError if it is gone."""
    projects = load()
    project = next((project for project in projects if project.name == name), None)
    if project is None:
        raise ValueError(f"проекта «{name}» уже нет")
    project.channels = list(channels)  # the names checked stay: a channel dropped and added back is named
    _save(projects)
    return project


def remember(name: str, infos) -> None:
    """Keep what a check on an account (scraper.scrape.lookup_chats) found out about the chats of the
    project's work — title, id, @username —, so its card and its flows' messages show them by name
    (chat_list); one not named, or no such project: nothing kept. Written only if it changed."""
    try:
        projects = load()
    except ValueError:  # an unreadable file: never overwritten
        return
    project = next((project for project in projects if project.name == name), None)
    if project is None:
        return
    found = {info.raw: {"title": info.title, "id": info.chat_id, "username": info.username}
             for info in infos if info.title}
    if all(project.chats.get(raw) == known for raw, known in found.items()):
        return
    project.chats.update(found)
    # not _save: names a check found are a cache, and .bak must stay the undo of the operator's last change
    json_file.save(PATH, [asdict(project) for project in projects])


def rename_problem(old: str, new: str, projects: list[Project]) -> str | None:
    """Why the project `old` can't be renamed to `new`, or None (a case change is fine)."""
    if new == old:
        return "имя не изменилось"
    return name_problem(new, [project for project in projects if project.name != old])


def rename(old: str, new: str) -> tuple[Project, int]:
    """Rename the project and everything of it on disk (scraped_files.project_entries), its
    unfinished scrape's checkpoint included: (the project, how many files and folders moved).
    ValueError with the reason — a bad name, a bot scrape of it under way, a name taken on disk —
    and then nothing is changed; a move that fails midway is undone."""
    projects = load()
    project = next((project for project in projects if project.name == old), None)
    if project is None:
        raise ValueError(f"проекта «{old}» уже нет")
    new = new.strip()
    if problem := rename_problem(old, new, projects):
        raise ValueError(problem)
    if home(project) in scraped_files.scrape_markers():  # the bot's scrape (running, or continued
        # after its restart) writes the old name's files
        raise ValueError("по проекту идёт скрап бота (или бот продолжит его после перезапуска) — "
                         "переименуйте, когда он закончится")
    moves = [(path, path.with_name(new + path.name[len(old):])) for path in scraped_files.project_entries(old)]
    if taken := [target.name for _, target in moves if target.exists()]:
        raise ValueError(f"на диске уже есть {', '.join(taken[:3])}{'…' if len(taken) > 3 else ''} — "
                         "выберите другое имя")
    done = []
    try:
        for source, target in moves:
            os.replace(source, target)
            done.append((source, target))
        _rename_checkpoint(old, new)
    except OSError as err:
        for source, target in reversed(done):
            os.replace(target, source)
        raise ValueError(f"файлы не переименованы ({err}) — всё возвращено как было") from err
    project.name = new
    _save(projects)
    return project, len(moves)


def _rename_checkpoint(old: str, new: str):
    """The unfinished scrape's resume.json: its name, and a top-up's previous posts file (extend)."""
    resume = Path(scraped_files.BASES_DIR) / f"{new}_partial" / "checkpoint" / "resume.json"
    try:
        meta = json_file.load(str(resume), None)
    except ValueError:  # a broken checkpoint: pending_resume ignores it too
        return
    if meta is None:
        return
    meta["name"] = new
    extend = Path(meta.get("extend") or "")
    if extend.name.startswith(f"{old}_"):
        meta["extend"] = str(extend.with_name(new + extend.name[len(old):]))
    json_file.save(str(resume), meta)


def remove(name: str):
    """Drop the project; its scraped files and its folder stay where they are. ValueError while it has
    workers: an account in no project must be one moved out of it on purpose (move_account)."""
    projects = load()
    project = next((project for project in projects if project.name == name), None)
    if project is not None and (count := len(account_files(sessions_dir(project)))):
        raise ValueError(f"у проекта аккаунтов: {count} — сначала перенесите их в другой проект или в свободные")
    kept = [project for project in projects if project.name != name]
    if len(kept) != len(projects):
        _save(kept)


def importable(limit: int = 30) -> list[tuple[str, list[str]]]:
    """(name, channels) of the finished scrapes a project can be made from, newest first, none a
    project already has: the name of their files (a members base's too) and the channels the newest
    posts file was scraped from (kept in the file, see verify_presets) — none without one.
    Reads the files: off the bot's event loop."""
    projects = load()
    found = []
    for name in scraped_files.scrape_names(limit):
        if name_problem(name, projects):
            continue
        posts = scraped_files.project_files(name, kinds=("posts",))
        found.append((name, list(scraped_files.verify_presets(posts[0][0])[0]) if posts else []))
    return found


# a list's line as the mailing takes it: a username (@name, or a t.me/name link) or a phone number
_USERNAME = re.compile(r"(?:@|(?i:(?:https?://)?(?:www\.)?(?:t\.me|telegram\.(?:me|dog))/))?([A-Za-z][A-Za-z0-9_]{3,31})")
_PHONE = re.compile(r"\+?\d{7,15}")


def as_username(line: str) -> str | None:
    """The line as "@username" (@name, name, a t.me/name link); None if it is no username."""
    found = _USERNAME.fullmatch(line.strip())
    return f"@{found[1]}" if found else None


def _recipient(line: str) -> str | None:
    """The line as "@username" or "+digits" (the mailing resolves a number as a contact); None if it is neither."""
    line = line.strip()
    if username := as_username(line):
        return username
    phone = re.sub(r"[\s()\-]", "", line)
    return "+" + phone.lstrip("+") if _PHONE.fullmatch(phone) else None


def add_to_list(name: str, lines: list[str]) -> tuple[int, int, int]:
    """Add a pasted list's or a .txt's lines to the project's list (scraped_files.list_path), its base for
    the mailing: (added, in it now, skipped — neither a username nor a number). One already in it, in
    any case, is not added again; the list is never replaced, as a group's members are added to its base."""
    path = scraped_files.list_path(name)
    current = scraped_files.read_list(path)
    known = {recipient.casefold() for recipient in current}
    added, skipped = [], 0
    for line in filter(str.strip, lines):  # a blank line is no recipient, nor a bad one
        recipient = _recipient(line)
        if recipient is None:
            skipped += 1
        elif recipient.casefold() not in known:
            known.add(recipient.casefold())
            added.append(recipient)
    if added:  # whole or not at all: a mailing may read it meanwhile
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_file.write(path, "".join(f"{recipient}\n" for recipient in current + added))
    return len(added), len(current) + len(added), skipped


FIRST_COLLECT = "сначала «▶️ Скрап проекта» или «👥 Участники чата»"
NO_CHANNELS_TEXT = "У проекта нет каналов — добавьте их: «➕ Добавить каналы»."  # a scrape needs them


def missing(project: Project, what: str) -> str | None:
    """Why the card's `what` (scrape, contacts, mail, verify, analysis) can't run on the project yet —
    what to collect first —, or None. The card shows them all, with data or not; reads the files' names
    and footers, so off the bot's event loop."""
    if what == "scrape":
        if project.channels or unfinished(project.name) is not None:
            return None
        return NO_CHANNELS_TEXT
    kinds = {label.partition(" ")[0] for _, label in scraped_files.project_files(
        project.name, kinds=("posts", "participants", "list"))}
    if what == "contacts" and "participants" not in kinds:
        return (f"У проекта ещё нет базы участников — {FIRST_COLLECT}. Загруженный список для контактов не "
                "подходит: в нём нет ID.")
    if what == "mail" and not kinds & {"participants", "list"}:
        return "У проекта ещё нет базы — сначала «▶️ Скрап проекта», «👥 Участники чата» или «📥 Загрузить список»."
    if what in ("verify", "analysis") and "posts" not in kinds:
        return "У проекта ещё нет файла постов — сначала «▶️ Скрап проекта»."
    return None


# what a project's card clears: the bases that are only ever added to (a group's members, the uploaded
# list); a scrape's own participants base is rebuilt from its posts, never cleared
CLEARABLE = {"members": scraped_files.members_base, "list": scraped_files.list_path}


def clear(name: str, what: str) -> Path:
    """Move the project's group members' base (`what` "members") or its list ("list") to
    <name>_previous/, where it can be taken back from (a top-up empties that folder: scraper.scrape's
    _keep_previous); one moved there before under the same name is replaced. Returns its new path;
    ValueError if there is nothing to clear."""
    path = CLEARABLE[what](name)
    if not path.exists():
        raise ValueError("очищать нечего — файла уже нет")
    keep = Path(scraped_files.BASES_DIR) / f"{name}_previous"
    keep.mkdir(exist_ok=True)
    return path.replace(keep / path.name)


@dataclass
class Topup:
    posts: str            # the previous run's posts file: the top-up merges into its files
    channels: list[str]   # as that scrape was given them
    keyword: str
    date_min: date
    date_max: date


def topup(project: Project) -> Topup | None:
    """The project's top-up (ScrapeParams.extend): the newest scrape's posts file's (not 🧩's: scraped_files.
    scrape_posts) channels and keyword, from TOPUP_OVERLAP before the end of its scrape window to today. None
    without a finished scrape that stored its window and channels (an older or a combined-by-hand file)."""
    posts = next((path for path, _ in scraped_files.scrape_posts(project.name)), None)  # not 🧩's file
    if posts is None:
        return None
    try:
        attrs = read_attrs(posts)
    except Exception:  # unreadable / half-written: nothing to top up
        return None
    window, channels = attrs.get("scrape_window"), attrs.get("channels")
    if not window or not channels:
        return None
    end = date.fromisoformat(window["date_max"])
    today = datetime.now(timezone.utc).date()  # the scrape's dates are UTC
    return Topup(posts, list(channels), attrs.get("keyword", ""), end - TOPUP_OVERLAP, max(today, end))


def unfinished(name: str) -> dict | None:
    """The project's interrupted scrape (its checkpoint's resume.json), or None."""
    return pending_resume(scraped_files.BASES_DIR, name)


def find(name: str | None) -> Project | None:
    """The project of that name; None without one, or with an unreadable file (load says why where it matters)."""
    try:
        return next((project for project in load() if project.name == name), None)
    except ValueError:
        return None


def home(project: Project) -> str:
    """The project's own folder: its workers (sessions/), proxies.txt, pm_mailing.json, contacts.json."""
    return os.path.join(PROJECTS_DIR, str(project.id))


def sessions_dir(project: Project) -> str:
    """Where the project's workers' session files are (a dead one goes to its inactive/, as before)."""
    return os.path.join(home(project), "sessions")


def storage(project: Project, api_id, api_hash, initialize: bool = False) -> SessionsStorage:
    """The project's workers, its folder as their home (their mailing stats, ledger, proxies)."""
    return SessionsStorage(sessions_dir(project), api_id, api_hash, initialize, home=home(project))


def proxies_path(project_home: str) -> str:
    """The project's proxy list: its new accounts take theirs from it, 🌐 Прокси deals it out to its workers."""
    return os.path.join(project_home, "proxies.txt")


def restricted_proxies_path(project_home: str) -> str:
    """The project's proxies for its permanently restricted workers (see SessionsStorage.rehome_restricted):
    off the pool, so their places go to new accounts. None set: they keep their pool proxies."""
    return os.path.join(project_home, "proxies_restricted.txt")


def account_files(directory: str) -> list[str]:
    """The session files right in `directory` (not in its inactive/ or restricted/), by name."""
    if not os.path.isdir(directory):
        return []
    return [os.path.join(directory, name) for name in sorted(os.listdir(directory))
            if name.endswith((".session", ".jsession"))]


def migrate():
    """Bring the projects from before they had their own accounts up to date; at every start of the
    bot and the CLI, a no-op once done. A project without an id gets one, and a copy of the shared
    mailing stats and contacts ledger (LEGACY_STATS) as its own; the shared ones stay as they are. The
    bot's scrape marker of then becomes its project's (scraped_files.scrape_markers). The workers stay
    in FREE_DIR: they are moved into projects by hand. ValueError if the projects' file can't be read
    (load)."""
    projects = load()
    old = [project for project in projects if not project.id]
    if not old:
        return
    next_id = max((project.id for project in projects), default=0) + 1
    for project in old:
        project.id, next_id = next_id, next_id + 1
        os.makedirs(home(project), exist_ok=True)
        for name, path in LEGACY_STATS.items():
            if os.path.exists(path):
                shutil.copy2(path, os.path.join(home(project), name))
    try:  # a bot scrape a restart interrupted, from when there was one: continued as its project's
        marker = json_file.load(scraped_files.SCRAPE_MARKER, None)
    except (OSError, ValueError):
        marker = None
    if isinstance(marker, dict) and "out_dir" in marker:
        owner = next((project for project in projects if project.name == marker.get("name")), None)
        json_file.save(scraped_files.SCRAPE_MARKER, {home(owner): marker} if owner is not None else {})
    _save(projects)


def _worker_dirs() -> list[str]:
    """Every folder a worker's session file can be in: the free ones', then each project's."""
    return [FREE_DIR, *sorted(str(path) for path in Path(PROJECTS_DIR).glob("*/sessions"))]


def _account(path: str) -> dict | None:
    """A .jsession's "account" part (read offline), None for a broken file or a .session (no metadata)."""
    if not path.endswith(".jsession"):
        return None
    try:
        return dict(json_file.load(path, {})["account"])
    except (OSError, ValueError, KeyError, TypeError):  # a broken file is skipped by SessionsStorage too
        return None


def find_account(phone: str | None = None, user_id: int | None = None) -> str | None:
    """The session file of the worker with this phone (with or without the +) or user id, wherever it
    is — free or in any project —, read offline; None if there is none. An account is in one place only."""
    phones = {phone.lstrip("+"), "+" + phone.lstrip("+")} if phone else set()
    for directory in _worker_dirs():
        for path in account_files(directory):
            account = _account(path)
            if account is not None and (account.get("phone_number") in phones
                                        or (user_id is not None and account.get("user_id") == user_id)):
                return path
    return None


def place(path: str) -> str:
    """Where a worker's session file is, for a message: «в проекте «X»» or «среди свободных (sessions/)»."""
    directory = os.path.dirname(os.path.normpath(path))
    if directory == os.path.normpath(FREE_DIR):
        return f"среди свободных ({FREE_DIR}/)"
    try:
        project = next((project for project in load() if os.path.normpath(sessions_dir(project)) == directory),
                       None)
    except ValueError:
        project = None
    return f"в проекте «{project.name}»" if project is not None else f"в {directory}/"


def taken(path: str) -> str:
    """Why an account can't be added here: it is a worker elsewhere, and how to bring it over."""
    if os.path.dirname(os.path.normpath(path)) == os.path.normpath(FREE_DIR):
        how = "возьмите его: «➕ Добавить» → «📦 Из свободных»"
    else:
        how = "перенесите его оттуда: «🤖 Аккаунты» → «↪️ Перенести»"
    return f"уже воркер {place(path)} — {how}"


def account_label(path: str) -> str:
    """A worker's file in a list, read offline: @username, else the name, and the phone."""
    account = _account(path) or {}
    name = (f"@{account['username']}" if account.get("username")
            else " ".join(filter(None, (account.get("first_name"), account.get("last_name")))))
    phone = account.get("phone_number")
    phone = f"+{phone.lstrip('+')}" if phone else os.path.basename(path)
    return f"{name} · {phone}" if name else phone


def move_account(path: str, target_dir: str) -> str:
    """Move a worker's session file to another project's sessions/ (or FREE_DIR), its restrictions
    (restricted_workers, keyed by the file's path) with it; returns its new path. ValueError, nothing
    moved, if a scrape on it is left to continue (its checkpoint names the old path) or the target
    has a file of that name. The caller takes it out of its live pool first, and checks it is not busy."""
    if scraped_files.unfinished_scrape(path):
        raise ValueError("на аккаунте незавершённый скрап — сначала завершите его")
    target = os.path.join(target_dir, os.path.basename(path))
    if os.path.exists(target):
        raise ValueError(f"там уже есть {os.path.basename(path)}")
    os.makedirs(target_dir, exist_ok=True)
    os.replace(path, target)
    restricted_workers.rename(path, target)
    return target


def chat_list(project: Project | None, raws: list[str], html: bool = False, problems: dict | None = None,
              limit: int = MAX_CARD_CHANNELS, budget: int | None = None) -> str:
    """The chats, numbered, one block each as the card lists its channels (scraper.scrape.chat_block): the
    title, @username or link, 🆔 — as the project's checks named them (Project.chats) —, ⚠️ and why from
    `problems` ({raw: why}). At most `limit` of them, whole blocks within `budget` characters (a cut one
    could cut a tag), then "… и ещё N". `html`: for the bot, else plain text for the terminal."""
    blocks, size = [], 0
    for number, raw in enumerate(raws[:limit], 1):
        info = project.chat(raw) if project is not None else ChatInfo(raw, None, None)
        block = chat_block(number, info._replace(problem=(problems or {}).get(raw)), html=html)
        size += len(block) + 2
        if budget is not None and size > budget:
            break
        blocks.append(block)
    if len(blocks) < len(raws):
        blocks.append(f"… и ещё {len(raws) - len(blocks)}")
    return "\n\n".join(blocks)


def label(project: Project) -> str:
    """The project in a list (bot button / CLI line); ⏸ marks an interrupted scrape."""
    channels = f"каналов: {len(project.channels)}" if project.channels else "без каналов"
    return f"📁 {project.name} · {channels}{' ⏸' if unfinished(project.name) else ''}"


def card(project: Project, workers: int, html: bool = False) -> str:
    """The project's card (bot and CLI): how many workers it has, its channels as the accounts list
    shows accounts (the names the last check found: remember), an interrupted scrape or the top-up,
    its files. `html`: for the bot — escaped, the ids in <code>; without it plain text, for the terminal."""
    esc = html_text.escape if html else str
    head = [f"📁 {esc(project.name)}",
            f"🤖 Воркеров: {workers}" if workers else
            "🤖 Воркеров нет — добавьте их: «🤖 Аккаунты» → «➕ Добавить» (по номеру, из tdata или из свободных).",
            f"📢 Каналы — {len(project.channels)}" if project.channels else
            "📢 Каналов нет: база — из участников групп (👥) или списка; для скрапа — «➕ Добавить каналы»."]
    blocks = []  # after the channels
    meta = unfinished(project.name)
    if meta is not None:
        blocks.append(f"⏸ Незавершённый скрап: собрано постов {meta.get('t_index', 0)} — "
                      "скрап проекта предложит продолжить.")
    elif (plan := topup(project)) is not None:
        text = (f"🔄 Дособор: с {plan.date_min:%d.%m.%Y} по {plan.date_max:%d.%m.%Y} — новое вольётся "
                f"в файлы проекта, последние {TOPUP_OVERLAP.days} дней прошлого скрапа перечитаются.")
        if plan.channels != project.channels:  # a top-up continues the previous scrape as it was
            text += ("\nКаналы дособора — как в прошлом скрапе (новому каналу нужна вся история: «▶️ Скрап "
                     f"проекта»):\n\n{chat_list(project, plan.channels, html, budget=MAX_CHANNELS_TEXT)}")
        blocks.append(text)
    files = scraped_files.project_files(project.name, kinds=("posts", "reactors", "participants", "list"))
    if files:
        lines = [f"📂 Файлы в {esc(scraped_files.BASES_DIR)} — {len(files)}",
                 *(f"• {esc(text)}" for _, text in files[:MAX_CARD_FILES])]
        if len(files) > MAX_CARD_FILES:
            lines.append(f"… и ещё {len(files) - MAX_CARD_FILES}")
        blocks.append("\n".join(lines))
    else:
        blocks.append("📂 Файлов скрапа пока нет.")
    # the channels last: in the bot they get what the rest leaves (long titles, forum topics), the
    # "… и ещё" line kept room for
    room = MAX_CARD_TEXT - len("\n\n".join(head + blocks)) - len("\n\n… и ещё 9999") if html else None
    channels = []
    for number, raw in enumerate(project.channels[:MAX_CARD_CHANNELS], 1):
        block = chat_block(number, project.chat(raw), UNKNOWN_CHAT, html)
        if room is not None:
            room -= len(block) + 2
            if room < 0:
                break
        channels.append(block)
    if len(channels) < len(project.channels):
        channels.append(f"… и ещё {len(project.channels) - len(channels)}")
    return "\n\n".join(head + channels + blocks)
