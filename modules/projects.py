"""Projects: named sets of scrape sources, kept in stats/projects.json and shared by the bot
and the CLI. A project's name is the name its scrapes run under, so its files are the
<name>_posts / _reactors / _participants ones in the bases folder (scraped_files.project_files)
and an interrupted scrape of it is continued by that name (scraper.scrape.pending_resume)."""
import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from datetime import date, datetime, timedelta, timezone

from modules import json_file, scraped_files
from scraper.datafiles import read_attrs
from scraper.scrape import pending_resume

PATH = os.path.join("stats", "projects.json")
MAX_NAME = 40  # a project's name is a button label and a file name prefix
# the card must stay within a Telegram message (4096): the channels' text and the files listed
MAX_CHANNELS_TEXT = 1500
MAX_CARD_FILES = 20
TOPUP_OVERLAP = timedelta(days=7)  # a top-up re-reads its last week: late comments and reactions under it


@dataclass
class Project:
    name: str
    channels: list[str]  # as typed: @name / t.me link / invite / numeric id (see parse_channels)


def load() -> list[Project]:
    """The projects; none without the file (an install without projects works as before).
    ValueError, with the reason, if the file can't be read: it is never overwritten then."""
    try:
        data = json_file.load(PATH, [])
        return [Project(name=item["name"], channels=list(item["channels"])) for item in data]
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
    """A new project; ValueError with the reason on a bad name or no channels."""
    projects = load()
    name = name.strip()
    if problem := name_problem(name, projects):
        raise ValueError(problem)
    if not channels:
        raise ValueError("нет ни одного канала")
    project = Project(name, list(channels))
    _save([*projects, project])
    return project


def set_channels(name: str, channels: list[str]) -> Project:
    """Replace the project's channels; ValueError if it is gone or the list is empty."""
    if not channels:
        raise ValueError("нет ни одного канала")
    projects = load()
    project = next((project for project in projects if project.name == name), None)
    if project is None:
        raise ValueError(f"проекта «{name}» уже нет")
    project.channels = list(channels)
    _save(projects)
    return project


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
    try:  # the bot's scrape (running, or continued after its restart) writes the old name's files
        marker = json_file.load(scraped_files.SCRAPE_MARKER, None)
    except (OSError, ValueError):
        marker = None
    if marker is not None and marker.get("name") == old:
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
    """Drop the project; its scraped files stay where they are."""
    projects = load()
    kept = [project for project in projects if project.name != name]
    if len(kept) != len(projects):
        _save(kept)


def importable(limit: int = 30) -> list[tuple[str, list[str]]]:
    """(name, channels) of the finished scrapes a project can be made from: a posts file's name and
    the channels it was scraped from (kept in the file, see verify_presets), newest first; none a
    project already has. Reads the files: off the bot's event loop."""
    projects = load()
    found = []
    for path, _ in scraped_files.posts_bases(limit):
        name = os.path.basename(path).partition("_posts")[0]
        if name_problem(name, projects) or any(name == known for known, _ in found):
            continue
        channels = scraped_files.verify_presets(path)[0]
        if channels:  # a file that can't tell its channels: nothing to make a project of
            found.append((name, list(channels)))
    return found


@dataclass
class Topup:
    posts: str            # the previous run's posts file: the top-up merges into its files
    channels: list[str]   # as that scrape was given them
    keyword: str
    date_min: date
    date_max: date


def topup(project: Project) -> Topup | None:
    """The project's top-up (ScrapeParams.extend): the newest posts file's channels and keyword,
    from TOPUP_OVERLAP before the end of its scrape window to today. None without a finished
    scrape that stored its window and channels (an older or a combined file)."""
    posts = next((path for path, text in scraped_files.project_files(project.name) if text.startswith("posts")),
                 None)
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


def label(project: Project) -> str:
    """The project in a list (bot button / CLI line); ⏸ marks an interrupted scrape."""
    return f"📁 {project.name} · каналов: {len(project.channels)}{' ⏸' if unfinished(project.name) else ''}"


def card(project: Project) -> str:
    """The project's card (bot and CLI): its channels, an interrupted scrape, its files."""
    channels = ", ".join(project.channels)
    if len(channels) > MAX_CHANNELS_TEXT:
        channels = channels[:MAX_CHANNELS_TEXT] + "…"
    lines = [f"📁 {project.name}", f"Каналы ({len(project.channels)}): {channels}"]
    meta = unfinished(project.name)
    if meta is not None:
        lines.append(f"⏸ Незавершённый скрап: собрано постов {meta.get('t_index', 0)} — "
                     "скрап проекта предложит продолжить.")
    elif (plan := topup(project)) is not None:
        lines.append(f"🔄 Дособор: с {plan.date_min:%d.%m.%Y} по {plan.date_max:%d.%m.%Y} — новое вольётся "
                     f"в файлы проекта, последние {TOPUP_OVERLAP.days} дней прошлого скрапа перечитаются.")
        if plan.channels != project.channels:  # a top-up continues the previous scrape as it was
            lines.append(f"   Каналы дособора — как в прошлом скрапе: {', '.join(plan.channels)[:MAX_CHANNELS_TEXT]} "
                         "(новому каналу нужна вся история: «▶️ Скрап проекта»).")
    files = scraped_files.project_files(project.name)
    if files:
        lines += [f"Файлы в {scraped_files.BASES_DIR}:", *(f"• {text}" for _, text in files[:MAX_CARD_FILES])]
        if len(files) > MAX_CARD_FILES:
            lines.append(f"… и ещё {len(files) - MAX_CARD_FILES}")
    else:
        lines.append("Файлов скрапа пока нет.")
    return "\n".join(lines)
