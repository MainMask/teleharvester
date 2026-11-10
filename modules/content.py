"""The content accounts are given — by 👤 Профиль's lists and by the warmup —, one set per region, the project's
(modules/warmup.region): assets/content/<region>/ — names.txt, usernames.txt, bios.txt, channels.txt, bots.txt,
stickers.txt, notes.txt (a line each, # comments), photos/ and music/ (files).
"""
import os

DIR = os.path.join("assets", "content")
REGIONS = {"ru": "🇷🇺 СНГ (RU)", "en": "🌍 Global (EN)"}


def path(region: str, name: str) -> str:
    """A list's or a folder's path: "names.txt", "photos"."""
    return os.path.join(DIR, region, name)


def pool(region: str, name: str) -> list[str]:
    """The lines of <region>/<name>.txt but blank ones and # comments; [] without one."""
    try:
        with open(path(region, f"{name}.txt"), encoding="utf-8-sig") as fileobj:
            return [line.strip() for line in fileobj if line.strip() and not line.lstrip().startswith("#")]
    except FileNotFoundError:
        return []


def files(region: str, folder: str) -> list[str]:
    """The files of <region>/<folder>/ (photos, music); hidden ones (.DS_Store) left out."""
    folder = path(region, folder)
    return sorted(os.path.join(folder, name) for name in os.listdir(folder)
                  if not name.startswith(".") and os.path.isfile(os.path.join(folder, name))) \
        if os.path.isdir(folder) else []
