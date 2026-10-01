from aiogram.fsm.state import State, StatesGroup


class PmBroadcast(StatesGroup):
    peer = State()
    text = State()


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


class SetPassword(StatesGroup):
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
    text = State()


class Comments(StatesGroup):
    link = State()
    text = State()


class Instant(StatesGroup):
    sticker = State()
    link = State()


class ChatBroadcast(StatesGroup):
    sticker = State()


class ReportMessage(StatesGroup):
    link = State()
    ids = State()
    comment = State()


class Scrape(StatesGroup):
    channels = State()
    name = State()
    out_dir = State()
    date_min = State()
    date_max = State()


class Verify(StatesGroup):
    input = State()
    channel = State()
    date_min = State()
    date_max = State()


class Analysis(StatesGroup):
    args = State()
