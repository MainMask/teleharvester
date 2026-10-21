from enum import Enum

from aiogram.filters.callback_data import CallbackData


class MenuAction(str, Enum):
    # the screens' actions (modules/registry.py SCREENS); a project's card's (bot/routers/projects/card.py):
    SCRAPE = "scrape"          # scrape the project's channels, or continue its unfinished scrape
    TOPUP = "topup"            # what is new since the last scrape, into the same files
    UPLOAD = "upload"          # add to the project's list of @usernames / links / numbers
    CHANNELS = "channels"      # the project's channels
    RENAME = "rename"          # the project's name, its files renamed with it
    CLEAR_BASE = "clearbase"   # its group members' base or its list, to <name>_previous/
    DELETE = "delete"          # the project, its files kept
    # the project's accounts'
    LIST = "list"              # poll and list the worker accounts
    PROXY = "proxy"            # open proxy setup on the workers screen
    ADD = "add"                # add an account: by phone number + code, or from tdata
    TDATA = "tdata"            # upload a tdata account (a choice of ADD)
    FREE = "free"              # take accounts in no project into this one (a choice of ADD)
    MOVE = "move"              # move the project's workers to another project or the free ones
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


class ReleaseCB(CallbackData, prefix="release"):
    user_id: int  # the permanently restricted worker whose contacts go to the other workers
