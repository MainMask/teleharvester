from enum import Enum

from aiogram.filters.callback_data import CallbackData


class MenuAction(str, Enum):
    WORKERS = "workers"        # back to the "🤖 Воркеры" screen
    LIST = "list"              # poll and list the worker accounts
    PROXY = "proxy"            # open proxy setup on the workers screen
    TDATA = "tdata"            # upload a tdata account on the workers screen
    DELAY = "delay"            # set the delay between actions (config.toml)


class MenuCB(CallbackData, prefix="menu"):
    action: MenuAction


class CategoryCB(CallbackData, prefix="cat"):
    index: int  # index into registry.WORKER_GROUPS


class FunctionCB(CallbackData, prefix="fn"):
    key: str  # registry key of the function to run


class ChoiceCB(CallbackData, prefix="ch"):
    scope: str  # which step the choice belongs to, e.g. "pm_mode", "pm_media", "join_mode"
    value: str
