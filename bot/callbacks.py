from enum import Enum

from aiogram.filters.callback_data import CallbackData


class MenuAction(str, Enum):
    # the screens' actions (modules/registry.py SCREENS); a project's card's (bot/routers/projects/card.py):
    SCRAPE = "scrape"          # scrape the project's channels, or continue its unfinished scrape
    TOPUP = "topup"            # what is new since the last scrape, into the same files
    UPLOAD = "upload"          # add to the project's list of @usernames / links / numbers
    CHANNELS = "channels"      # the project's channels
    REGION = "region"          # the project's region: its content's (modules/content.py), cycled
    RENAME = "rename"          # the project's name, its files renamed with it
    CLEAR_BASE = "clearbase"   # its group members' base or its list, to <name>_previous/
    DELETE = "delete"          # the project, its files kept
    # the project's accounts'
    PROXY = "proxy"            # open proxy setup on the workers screen
    MOVE = "move"              # move the project's workers to another project or the free ones
    POLL = "profiles"          # every worker asked for its profile (📋 Список's)
    # ➕ Добавить's: an account added
    BY_PHONE = "byphone"       # a worker, by phone number + code
    PERSONAL = "personal"      # a personal account (all the projects', scrapes only), by phone number + code
    FILES = "files"            # workers uploaded: tdata ZIPs, .session (+ .json) files
    FREE = "free"              # accounts in no project taken into this one
    # the settings'
    DELAY = "delay"            # set the delay between actions (config.toml)
    PROFILE_PAUSE = "ppause"   # set the pause between accounts for profile changes (config.toml)
    CAP_PM = "cap_pm"              # the per-account daily caps (config.toml [limits]): PM mailing,
    CAP_INVITE = "cap_invite"      # inviting,
    CAP_CONTACTS = "cap_contacts"  # adding to contacts


# A project's screen's button carries its project (`project`, its id): it acts on that project, whichever the
# chat is in now — the chat goes into it (bot/services/workspaces.py ProjectMiddleware); 0: the current one.

class MenuCB(CallbackData, prefix="menu"):
    action: MenuAction
    project: int = 0


class NavCB(CallbackData, prefix="nav"):
    screen: str  # the title of the screen to open (a key of registry.SCREENS), its ⬅️ Назад too
    project: int = 0
    fresh: bool = False  # as a new message: the button's own (a job's report) stays as it is


class FunctionCB(CallbackData, prefix="fn"):
    key: str  # registry key of the function to run
    project: int = 0


class ChoiceCB(CallbackData, prefix="ch"):
    scope: str  # which step the choice belongs to, e.g. "pm_mode", "pm_media", "join_mode"
    value: str


class WarmupCB(CallbackData, prefix="wu"):
    """🔥 Прогрев's own buttons (bot/routers/warmup.py): a setting's next value, the reset of the warmup."""
    action: str  # "set" (value: a setting or a step), "threads", "reset", "resetok"
    value: str = ""  # the setting or the step (modules/warmup.py)
    project: int = 0


class AutoreplyCB(CallbackData, prefix="ar"):
    """📬 Автоответ' own buttons (bot/routers/autoreply.py): its on/off, its text, every worker's replies."""
    action: str  # "toggle", "text", "workers"
    project: int = 0


class AccountBaseCB(CallbackData, prefix="ab"):
    """📋 Список's own buttons (bot/routers/accounts/base.py): a category's accounts, its ZIP, its deletion."""
    action: str  # "cat", "zip", "del", "delok"
    value: str = ""  # the category (modules/warmup.CATEGORIES, or PERSONAL), or "all" for the whole base's ZIP
    project: int = 0
    fmt: str = ""  # a ZIP's format (base.FORMATS); "": asked first


class ReleaseCB(CallbackData, prefix="release"):
    user_id: int  # the permanently restricted worker whose contacts go to the other workers
