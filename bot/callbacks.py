from enum import Enum

from aiogram.filters.callback_data import CallbackData


class MenuAction(str, Enum):
    # the screens' actions (modules/registry.py SCREENS)
    LIST = "list"              # poll and list the worker accounts
    PROXY = "proxy"            # open proxy setup on the workers screen
    ADD = "add"                # add an account: by phone number + code, or from tdata
    TDATA = "tdata"            # upload a tdata account (a choice of ADD)
    DELAY = "delay"            # set the delay between actions (config.toml)
    PROFILE_PAUSE = "ppause"   # set the pause between accounts for profile changes (config.toml)
    AUTOREPLY = "autoreply"    # the auto-reply to PM-mailing replies: on/off, text, stats


class MenuCB(CallbackData, prefix="menu"):
    action: MenuAction


class NavCB(CallbackData, prefix="nav"):
    screen: str  # the title of the screen to open (a key of registry.SCREENS), its ⬅️ Назад too


class FunctionCB(CallbackData, prefix="fn"):
    key: str  # registry key of the function to run


class ChoiceCB(CallbackData, prefix="ch"):
    scope: str  # which step the choice belongs to, e.g. "pm_mode", "pm_media", "join_mode"
    value: str


class ReleaseCB(CallbackData, prefix="release"):
    user_id: int  # the permanently restricted worker whose contacts go to the other workers
