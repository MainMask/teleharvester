"""Projects: named sets of scrape sources, kept in stats/projects.json and shared by the bot
and the CLI. A project's name is the name its scrapes run under, so its files are the
<name>_posts / _reactors / _participants ones in the bases folder (scraped_files.project_files)
and an interrupted scrape of it is continued by that name (scraper.scrape.pending_resume)."""
import os
import shutil
from dataclasses import asdict, dataclass

from modules import json_file, scraped_files
from scraper.scrape import pending_resume

PATH = os.path.join("stats", "projects.json")
MAX_NAME = 40  # a project's name is a button label and a file name prefix
MAX_CHANNELS_TEXT = 1500  # the card must stay within a Telegram message
FILE_MARKERS = ("_posts", "_reactors", "_participants", "_partial")


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
    """Why `name` can't be a new project's name, or None."""
    if not name:
        return "имя пустое"
    if len(name) > MAX_NAME:
        return f"имя длиннее {MAX_NAME} символов"
    if "/" in name or "\\" in name or name in (".", ".."):
        return "имя станет частью имени файла: без / и \\"
    # the scrape's file names are cut at these (scraped_files.project_files, pending_resume)
    if marker := next((m for m in FILE_MARKERS if m in name), None):
        return f"в имени не должно быть «{marker}» — так размечаются файлы скрапа"
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


def unfinished(name: str) -> dict | None:
    """The project's interrupted scrape (its checkpoint's resume.json), or None."""
    return pending_resume(scraped_files.BASES_DIR, name)


def label(project: Project) -> str:
    """The project in a list (bot button / CLI line); ⏸ marks an interrupted scrape."""
    return f"{project.name} · каналов: {len(project.channels)}{' ⏸' if unfinished(project.name) else ''}"


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
    files = scraped_files.project_files(project.name)
    if files:
        lines += [f"Файлы в {scraped_files.BASES_DIR}:", *(f"• {text}" for _, text in files)]
    else:
        lines.append("Файлов скрапа пока нет.")
    return "\n".join(lines)
