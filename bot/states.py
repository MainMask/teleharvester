from aiogram.fsm.state import State, StatesGroup


class PmBroadcast(StatesGroup):
    peer = State()
    message = State()


class Invite(StatesGroup):
    source = State()
    destination = State()


class Join(StatesGroup):
    link = State()


class ChangeName(StatesGroup):
    manual = State()


class ChangeUsername(StatesGroup):
    base = State()


class ChangeBio(StatesGroup):
    text = State()


class ChangePhoto(StatesGroup):
    photo = State()


class SetPassword(StatesGroup):
    password = State()


class SetProxy(StatesGroup):
    input = State()


class SetDelay(StatesGroup):
    input = State()


class SetProfilePause(StatesGroup):
    input = State()


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


class Comments(StatesGroup):
    link = State()
    message = State()


class Instant(StatesGroup):
    sticker = State()
    link = State()
    message = State()


class ChatBroadcast(StatesGroup):
    sticker = State()
    message = State()


class BroadcastOptions(StatesGroup):
    """Steps shared by the chat / instant / comments flows; FSM data "flow" says which one."""
    trigger = State()
    count = State()


class ReportMessage(StatesGroup):
    link = State()
    ids = State()
    comment = State()


class Scrape(StatesGroup):
    name = State()
    out_dir = State()
    resume = State()
    channels = State()
    date_min = State()
    date_max = State()
    confirm = State()
    keyword = State()
    max_messages = State()


class Members(StatesGroup):
    chats = State()
    name = State()


class Verify(StatesGroup):
    input = State()
    channel = State()
    date_min = State()
    date_max = State()


class Analysis(StatesGroup):
    file = State()
    keywords = State()
