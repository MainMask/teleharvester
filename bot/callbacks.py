from aiogram.filters.callback_data import CallbackData


class MenuCB(CallbackData, prefix="menu"):
    action: str  # "functions" | "accounts" | "home"


class CategoryCB(CallbackData, prefix="cat"):
    index: int  # index into registry.categories()


class FunctionCB(CallbackData, prefix="fn"):
    key: str  # registry key of the function to run


class ChoiceCB(CallbackData, prefix="ch"):
    scope: str  # which step the choice belongs to, e.g. "pm_mode", "pm_media", "join_mode"
    value: str
