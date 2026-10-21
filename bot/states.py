from aiogram.fsm.state import State, StatesGroup

from modules.registry import (ACCOUNTS, ACTIVITY, AUTOREPLY, BASE, BOT_FUNCTIONS_BY_KEY, COLLECT, MANAGE, PROFILE,
                              PROJECTS, SCREENS, SECURITY, SETTINGS)


class PmBroadcast(StatesGroup):
    peer = State()  # a @username, a link or a number: which, the bot sees (pm_broadcast.is_phone)
    message = State()


class Invite(StatesGroup):
    source = State()
    destination = State()


class Join(StatesGroup):
    mode = State()
    link = State()


class PickWorkers(StatesGroup):
    marks = State()


class MoveWorkers(StatesGroup):
    free = State()    # the free accounts ticked into the project
    out = State()     # the project's workers ticked to move out
    target = State()  # where they go


class ProfileText(StatesGroup):
    text = State()  # a name, a username's base or a bio (FSM data "pick_key" says which): typed or a list


class ChangePhoto(StatesGroup):
    photo = State()


class SetPassword(StatesGroup):
    password = State()
    confirm = State()


class SetProxy(StatesGroup):
    input = State()
    spare = State()  # the optional proxies for the permanently restricted workers, after the pool's


class SetSetting(StatesGroup):
    value = State()  # FSM data "setting": which one (bot/routers/settings.py)


class SetAutoreplyText(StatesGroup):
    input = State()


class AddByPhone(StatesGroup):
    phone = State()
    code = State()
    password = State()


class ImportTdata(StatesGroup):
    archive = State()
    password = State()


class Reactions(StatesGroup):
    link = State()


class PollVote(StatesGroup):
    link = State()
    option = State()


class AddContacts(StatesGroup):
    path = State()


class ReportUser(StatesGroup):
    username = State()
    comment = State()


class PmMailing(StatesGroup):
    path = State()
    limit = State()
    message = State()


class ChatPost(StatesGroup):
    """🗨 Комментарии, ⚡ Чат — сразу, 🔔 Чат — по слову (bot/routers/chat_broadcasts.py); FSM data "flow" says which."""
    mode = State()  # stays through the mentions' taps
    sticker = State()
    link = State()
    trigger = State()
    count = State()
    message = State()


class ReportMessage(StatesGroup):
    link = State()
    ids = State()
    comment = State()


class Scrape(StatesGroup):
    resume = State()
    channels = State()
    date_min = State()
    date_max = State()
    confirm = State()
    keyword = State()
    max_messages = State()


class Projects(StatesGroup):
    name = State()
    channels = State()
    edit = State()
    rename = State()
    upload = State()


class Members(StatesGroup):
    chats = State()


class Verify(StatesGroup):
    input = State()
    channel = State()
    date_min = State()
    date_max = State()


class Analysis(StatesGroup):
    file = State()
    keywords = State()


# where each form was started from: its ✖️ Отмена and /cancel lead back there (bot/routers/menu.py)
_ORIGINS = {
    ACTIVITY: (PmBroadcast, Invite, Join, Reactions, PollVote, ReportUser, ChatPost, ReportMessage),
    ACCOUNTS: (MoveWorkers, SetProxy, AddByPhone, ImportTdata),
    SECURITY: (SetPassword,),
    PROFILE: (ProfileText, ChangePhoto),
    AUTOREPLY: (SetAutoreplyText,),
    COLLECT: (Scrape, Members, Verify, Analysis),
    BASE: (AddContacts, PmMailing),
}
ORIGINS = {group.__name__: screen for screen, groups in _ORIGINS.items() for group in groups}


def origin(step: str | None, data: dict) -> str | None:
    """The screen the form at `step` (a state's name, "Group:step") was started from; None: the main menu's."""
    group, _, name = (step or "").partition(":")
    if group == PickWorkers.__name__:  # a profile's or a security's function: its own screen
        function = BOT_FUNCTIONS_BY_KEY.get(data.get("pick_key"))
        return function and function.category
    if group == SetSetting.__name__:  # the screen of the setting's button: ⚙️ Настройки or 📏 Дневные лимиты
        return next((title for title, screen in SCREENS.items() for button in screen.buttons
                     if button.action == data.get("setting")), SETTINGS)
    if group == Projects.__name__:  # a new project's name and channels: the list; the others: their screen
        return {"name": PROJECTS, "channels": PROJECTS, "upload": COLLECT}.get(name, MANAGE)
    return ORIGINS.get(group)
