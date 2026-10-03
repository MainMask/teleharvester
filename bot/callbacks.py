from enum import Enum

from aiogram.filters.callback_data import CallbackData


class MenuAction(str, Enum):
    CATEGORIES = "categories"  # back to the category list
    PROXY = "proxy"            # open proxy setup on the accounts screen
    TDATA = "tdata"            # upload a tdata account on the accounts screen


class MenuCB(CallbackData, prefix="menu"):
    action: MenuAction


class CategoryCB(CallbackData, prefix="cat"):
    index: int  # index into registry.categories()


class FunctionCB(CallbackData, prefix="fn"):
    key: str  # registry key of the function to run


class ChoiceCB(CallbackData, prefix="ch"):
    scope: str  # which step the choice belongs to, e.g. "pm_mode", "pm_media", "join_mode"
    value: str
