from dataclasses import dataclass

SAFE = "safe"
RISKY = "risky"


@dataclass(frozen=True)
class BotFunction:
    key: str        # callback id, used in FunctionCB
    classname: str  # TelethonFunction subclass name, as discovered from functions/; "" for one run by its own
                    # router (bot/routers/scraping/)
    title: str      # label shown in the menu
    risk: str       # SAFE or RISKY
    category: str   # menu category
    hint: str = ""  # one line under the title in the section message: what it does
    group: str = ""  # a subheading within its section's message


@dataclass(frozen=True)
class Button:
    """A screen's button that is neither a function nor a screen of its own."""
    action: str  # a MenuAction value (bot/callbacks.py)
    title: str
    hint: str    # one line next to its title on its screen: what it does
    group: str = ""  # a subheading within its screen's message, as BotFunction.group


@dataclass(frozen=True)
class Screen:
    title: str
    hint: str         # one line next to its button: in the /start message, or on its parent screen
    parent: str = ""  # the screen its ⬅️ Назад goes to (and its menu path); "" for a main-menu button
    buttons: tuple[Button, ...] = ()  # its actions, before its functions (BOT_FUNCTIONS of its category)
    keeps_form: bool = False  # only shows: opened, a form under way stays at its step (as /status does)
    own_buttons: bool = False  # its view adds buttons of its own (screen_kb's `own`), none risky: see all_risky
    refresh: bool = False  # its view shows live state: 🔄 Обновить next to its ⬅️ Назад (screen_kb)


# The whole menu lives here: the bot's keyboards, its screens' texts and the menu
# paths in messages (menu_path) are built from it, so a button can't lose its description.
# One tree of screens: each holds its actions, then its functions (under their subheadings), then
# the screens whose parent it is. The main menu (reply keyboard): the projects — all the work is a
# project's: its card opens the work on its files (the scrape and the bases, the mailing, the project
# itself) and on its workers (its accounts, their activity) —, the settings, the jobs running now. Every
# function has one place in it. How the bot shows a screen: bot/services/screens.py.
# what the delay and the pause between accounts hold up: the settings' screen and their prompts say the same
DELAY_HINT = "пауза между действиями воркера: рассылки, вступления, реакции, опросы, жалобы, инвайтинг, контакты"
PROFILE_PAUSE_HINT = "пауза между воркерами при смене имени, username, bio, фото, 2FA, канала и визита"
PROJECTS = "📁 Проекты"  # the projects: each one's card runs its scrape, the work on its bases, its accounts
CARD = "📁 Проект"  # a project's card (bot/routers/projects/), the parent of its screens
CARD_IN_PATH = "проект"  # how menu_path names the card: it is opened by the project's own name
COLLECT = "📥 Сбор"
BASE = "🎯 База и рассылка"
MANAGE = "🛠 Управление"
ADD_CHANNELS = "➕ Добавить каналы"  # 📢 Каналы' title for a project without channels
ACTIVITY = "💬 Активность"
ACCOUNTS = "🤖 Аккаунты"
ADD = "➕ Добавить"
POLL = "📡 Опросить"  # 📋 Список' button (bot/routers/accounts/base.py)
ACCOUNT_LIST = "📋 Список"
PROFILE = "👤 Профиль"
SECURITY = "🔐 Безопасность"
WARMUP = "🔥 Прогрев"
WARMUP_STEPS = "🧩 Шаги"
SETTINGS = "⚙️ Настройки"
AUTOREPLY = "📬 Автоответ"
HYPOTHESES = "🧪 Гипотезы"
TASKS = "📊 Задачи"
CHECKS_GROUP = "Проверка"  # 📋 Список's checks: its poll (an action) and @SpamBot's (a function), one row
PAUSES_GROUP = "Паузы"  # the settings' subheadings
CAPS_GROUP = "Дневные лимиты на аккаунт"
SCREENS = {screen.title: screen for screen in (
    # its list and cards: bot/routers/projects/
    Screen(PROJECTS, "ваши проекты: у каждого свои аккаунты, прокси, база и вся работа с ними"),
    Screen(CARD, "сбор, база и рассылка, аккаунты и их прогрев, активность, управление проектом", PROJECTS),
    # the card's screens, in the order of the work: collect and check the scrape, then the work on the base
    Screen(COLLECT, "каналы, скрап, участники групп; проверка и анализ файлов скрапа", CARD, buttons=(
        Button("channels", "📢 Каналы", "список каналов и групп проекта для скрапа"),
        Button("scrape", "▶️ Скрап", "посты каналов проекта за период: комментарии, реакции, участники"),
        Button("topup", "⏩ Дособрать", "новое с прошлого скрапа, в те же файлы"),
    )),
    # the base: what it holds (its view: bot/routers/projects/views.py), its list, its clearing, then the work on it
    Screen(BASE, "люди для рассылки: свой список, очистка, контакты воркеров, ЛС, автоответ", CARD, buttons=(
        Button("upload", "📄 Свой список", "@username, ссылки t.me или номера: список для рассылки"),
        Button("exportbase", "📦 Скачать базу", "вся база проекта одним файлом .csv — каждый человек один раз"),
        Button("clearbase", "🧹 Очистить базу", "участники групп или загруженный список уходят в <имя>_previous/, "
               "оттуда их можно вернуть"),
    )),
    # its view: bot/routers/autoreply.py
    Screen(AUTOREPLY, "ответ тем, кто ответил на рассылку в ЛС, и их сообщения сюда", BASE, own_buttons=True,
           refresh=True),
    # its view: bot/routers/hypotheses.py
    Screen(HYPOTHESES, "гипотезы рассылки: отправлено, ответили за 72 ч, итоги; отчёт .md для анализа", BASE,
           own_buttons=True, refresh=True),
    # its view (the warmup's line): bot/routers/accounts/base.py
    Screen(ACCOUNTS, "прокси, перенос; добавление, список и проверки, прогрев, профиль, безопасность", CARD, buttons=(
        Button("proxy", "🌐 Прокси", "задать список прокси проекта и раздать его аккаунтам"),
        Button("move", "↪️ Перенести", "воркеров в другой проект или в свободные"),
    )),
    # by phone: bot/routers/login.py; from files: accounts/upload.py; the free ones: accounts/move.py
    Screen(ADD, "по номеру, из файлов (tdata, .session) или из свободных аккаунтов", ACCOUNTS, buttons=(
        Button("byphone", "🤖 Воркер по номеру", "вход по номеру и коду; работает в этом проекте"),
        Button("personal", "👤 Личный по номеру", "вход по номеру и коду; общий для всех проектов, только для скрапа"),
        Button("files", "📥 Из файлов", "ZIP с tdata или файлы .session (+ .json) — воркерами проекта"),
        Button("free", "📦 Из свободных", "аккаунты, ещё ни в одном проекте, — воркерами проекта"),
    )),
    # the accounts' state: its checks, then (its view's own buttons) the categories, the personal accounts, the ZIP
    # — bot/routers/accounts/base.py, 📡 with workers only
    Screen(ACCOUNT_LIST, "опрос и проверка SpamBot; аккаунты по категориям и личные: карточки, ZIP, удаление",
           ACCOUNTS, buttons=(
               Button("profiles", POLL, "профиль и страна каждого воркера (запрос к Telegram), его ограничения по "
                      "последней проверке SpamBot", CHECKS_GROUP),
           ), own_buttons=True, refresh=True),
    # their views: bot/routers/warmup.py
    Screen(WARMUP, "оформление и ежедневная активность аккаунтов перед работой", ACCOUNTS, own_buttons=True,
           refresh=True),
    Screen(WARMUP_STEPS, "что делает прогрев: профиль, безопасность, активность; шансы и количества", WARMUP,
           own_buttons=True),
    Screen(PROFILE, "имя, username, bio, фото, канал в профиле", ACCOUNTS),
    Screen(SECURITY, "2FA, чужие сессии, видимость, очистка диалогов", ACCOUNTS),
    Screen(ACTIVITY, "все воркеры проекта по одной цели: ЛС, комментарии, чаты, вступления, реакции, опросы, "
                     "инвайтинг, жалобы", CARD),
    Screen(MANAGE, "регион, имя, удаление проекта", CARD, buttons=(
        Button("region", "🌍 Регион", "язык и страна имён, username, bio, фото и контента прогрева"),
        Button("rename", "🏷 Переименовать", "новое имя проекта, его файлы переименуются вместе с ним"),
        Button("delete", "🗑 Удалить", "проект удаляется, файлы скрапа остаются; воркеров сначала перенесите"),
    )),
    # its view: bot/routers/menu.py, as /status
    Screen(TASKS, "что сейчас выполняется во всех проектах: ход задач и их остановка", keeps_form=True,
           refresh=True),
    # its view (the values on its buttons): bot/routers/settings.py
    Screen(SETTINGS, "задержки и дневные лимиты на аккаунт — общие для всех проектов", buttons=(
        Button("delay", "⏱ Задержка", DELAY_HINT, PAUSES_GROUP),
        Button("ppause", "⏳ Пауза профиля", PROFILE_PAUSE_HINT, PAUSES_GROUP),
        Button("cap_pm", "📨 ЛС", "личных сообщений рассылки", CAPS_GROUP),
        Button("cap_invite", "🤝 Инвайты", "приглашений в чат", CAPS_GROUP),
        Button("cap_contacts", "📇 Контакты", "добавлений в контакты", CAPS_GROUP),
        Button("cap_comments", "🗨 Комменты", "комментариев под постами — к посту и к новым постам вместе",
               CAPS_GROUP),
    )),
)}
MAIN_MENU = tuple(title for title, screen in SCREENS.items() if not screen.parent)  # the reply keyboard


def children(title: str) -> list[str]:
    """The screens opened from this one, in SCREENS order."""
    return [child for child, screen in SCREENS.items() if screen.parent == title]


def trail(title: str) -> list[str]:
    """The screens from the main menu down to this one."""
    parent = SCREENS[title].parent
    return [*(trail(parent) if parent else []), title]


def in_project(title: str) -> bool:
    """The screen is a project's: its buttons act on that project (bot/callbacks.py: their `project`)."""
    return CARD in trail(title)


# the card's screens on the project's files: their buttons run on the project itself (bot/routers/projects/
# card.py); the card's others (🤖, 💬) run on its workers
PROJECT_FILES = (COLLECT, BASE, MANAGE)
ON_WORKERS = ("addcontacts", "pmmailing")  # the card's functions run by the workers, not on one account


# the subheadings of the screens' functions
ONE_TARGET = "Написать — пишут все воркеры"
IN_CHATS = "Действия в чатах"
REPORTS = "Жалобы"

# Every function exposed through the bot, in menu order. All run on worker accounts; RISKY ones
# additionally require at least one worker (see WorkerPool). Scraping entries run
# through their own router (not WorkerPool) but still use a worker's session string.
BOT_FUNCTIONS = [
    # a project's card (bot/routers/projects/card.py) runs these on the project's files:
    # 📥 Сбор — a group's members as a base, then the check of the scrape's files
    BotFunction("members", "", "👥 Участники групп", SAFE, COLLECT,
                "весь список участников групп, включая молчащих; сразу база для рассылки"),
    BotFunction("verify", "", "🔍 Верификация", SAFE, COLLECT,
                "сверяет файл скрапа с каналом и находит пропущенные посты"),
    BotFunction("analysis", "", "📈 Анализ", SAFE, COLLECT,
                "утилиты для файлов скрапа: ссылки на каналы, поиск по словам, просмотр"),
    # 🎯 База и рассылка: the contacts first — a mailing then writes to each person from the worker that
    # added them
    BotFunction("addcontacts", "AddContactsFunc", "📇 В контакты", RISKY, BASE,
                "добавляет людей из базы в контакты воркеров; рассылка потом пишет им с того же воркера"),
    BotFunction("pmmailing", "PmMailingFunc", "📨 Рассылка", RISKY, BASE,
                "личные сообщения людям из скрапнутой базы или списка; помнит, кому уже писали, и делит людей "
                "между воркерами"),
    # 💬 Активность: every worker writes to one target, then the actions in chats, then the reports
    BotFunction("pm", "PmBroadcastFunc", "✉️ ЛС человеку", RISKY, ACTIVITY,
                "все воркеры пишут одному человеку (по username или номеру)", ONE_TARGET),
    BotFunction("comments", "CommentsBroadcastFunc", "🗨 Комментарии — к посту", RISKY, ACTIVITY,
                "воркеры пишут комментарии под постом канала", ONE_TARGET),
    BotFunction("watch", "CommentsWatchFunc", "🆕 Комментарии — к новым постам", RISKY, ACTIVITY,
                "воркеры следят за каналами и по очереди комментируют каждый новый пост — до ⏹", ONE_TARGET),
    BotFunction("instant", "InstantBroadcastFunc", "⚡ Чат — сразу", RISKY, ACTIVITY,
                "воркеры сразу отправляют сообщения в чат или группу", ONE_TARGET),
    BotFunction("chat", "BroadcastChatFunc", "🔔 Чат — по слову", RISKY, ACTIVITY,
                "воркеры ждут кодовое слово в чате и после него начинают писать", ONE_TARGET),
    BotFunction("join", "JoinerFunc", "🚪 Вступить в чат", RISKY, ACTIVITY,
                "воркеры вступают в канал/группу, умеют проходить капчу", IN_CHATS),
    BotFunction("reactions", "ReactionsFunc", "❤️ Реакции", RISKY, ACTIVITY,
                "каждый воркер ставит реакцию на сообщение или пост", IN_CHATS),
    BotFunction("poll", "PollVoteFunc", "🗳 Голосовать", RISKY, ACTIVITY,
                "воркеры голосуют за выбранный вариант", IN_CHATS),
    BotFunction("invite", "InvitingFunc", "🤝 Инвайтинг", RISKY, ACTIVITY,
                "приглашает участников одного чата в другой", IN_CHATS),
    BotFunction("report", "ReportFunc", "🚩 Жалоба на пост", RISKY, ACTIVITY,
                "жалоба на посты; путь жалобы выбираете один раз, остальные воркеры повторяют", REPORTS),
    BotFunction("reportuser", "ReportUserFunc", "🚫 Жалоба на пользователя", RISKY, ACTIVITY,
                "жалоба на аккаунт от всех воркеров", REPORTS),
    # 🤖 Аккаунты → 📋 Список: the check of the accounts, after its poll
    BotFunction("status", "SpamBlockFunc", "🩺 SpamBot", SAFE, ACCOUNT_LIST,
                "спрашивает @SpamBot об ограничениях; бессрочно ограниченных исключает из рассылки, мёртвые сессии "
                "убирает", CHECKS_GROUP),
    # 🔥 Прогрев
    BotFunction("warmup", "WarmupFunc", "▶️ Прогреть", RISKY, WARMUP,
                "день прогрева аккаунтам, ещё не прогретым и не прогревавшимся сегодня"),
    # 🤖 Аккаунты → 👤 Профиль
    BotFunction("name", "ChangeNameFunc", "✏️ Имя", SAFE, PROFILE,
                "одно имя выбранным воркерам или случайное из списка"),
    BotFunction("username", "ChangeUsernameFunc", "🔤 Username", SAFE, PROFILE,
                "по основе со случайным числом (base24, base3071…) или из списка"),
    BotFunction("bio", "ChangeBioFunc", "📝 Bio", SAFE, PROFILE,
                "один текст «о себе» выбранным воркерам или случайный из списка"),
    BotFunction("photo", "ChangeProfilePhotoFunc", "🖼 Фото", SAFE, PROFILE,
                "одно фото выбранным воркерам или случайные из photos/ региона проекта"),
    BotFunction("clearchannel", "ClearPersonalChannelFunc", "📺 Убрать канал", SAFE, PROFILE,
                "снимает личный канал, закреплённый в профиле"),
    # 🤖 Аккаунты → 🔐 Безопасность: the account's protection and privacy, then the irreversible wipe
    BotFunction("2fa", "SetPasswordFunc", "🔒 Пароль 2FA", SAFE, SECURITY,
                "ставит облачный пароль выбранным воркерам"),
    BotFunction("terminate", "TerminateSessionsFunc", "📴 Чужие сессии", SAFE, SECURITY,
                "выкидывает из аккаунта все устройства, кроме бота"),
    BotFunction("lastseen", "HideLastSeenFunc", "🙈 Скрыть визит", SAFE, SECURITY,
                "«был(а) недавно» вместо точного времени"),
    BotFunction("clear", "ClearDialogsFunc", "🧹 Очистить диалоги", RISKY, SECURITY,
                "удаляет переписки у обеих сторон и выходит из всех каналов. Необратимо"),
]

BOT_FUNCTIONS_BY_KEY = {function.key: function for function in BOT_FUNCTIONS}


RISK_NOTE = "⚠️ — действия от имени воркеров: при частом использовании Telegram может ограничить аккаунт."


def all_risky(title: str) -> bool:
    """Every button of the screen acts on behalf of the workers: ⚠️ once, on its title. Its actions (Button:
    a list's upload, a base's clearing…) and its view's own buttons never do, so a screen with any is marked on
    its risky functions."""
    screen, functions = SCREENS[title], by_category(title)
    return bool(functions) and not (screen.buttons or screen.own_buttons) and all(f.risk == RISKY for f in functions)


def risk_marker(function: BotFunction) -> str:
    """ ⚠️ after a risky function's title, as after a screen's — unless its whole screen is risky (all_risky): the
    mark is then once, on the screen's title, and marking every button would say nothing."""
    return " ⚠️" if function.risk == RISKY and not all_risky(function.category) else ""


@dataclass(frozen=True)
class Entry:
    """A screen's button as its message and its keyboard show it (bot/services/screens.py, bot/keyboards/menu.py)."""
    key: str        # a Button's action, or a BotFunction's key
    label: str
    hint: str
    group: str
    marker: str     # risk_marker
    function: bool  # a BotFunction (FunctionCB), else a screen's action (MenuCB)


def entries(title: str, hidden=(), titles: dict | None = None) -> list[Entry]:
    """The screen's actions (`titles` in place of their own), then its functions; without the `hidden` (their action
    or key)."""
    titles = titles or {}
    return [*(Entry(button.action, titles.get(button.action, button.title), button.hint, button.group, "", False)
              for button in SCREENS[title].buttons if button.action not in hidden),
            *(Entry(function.key, function.title, function.hint, function.group, risk_marker(function), True)
              for function in by_category(title) if function.key not in hidden)]


def _place(key: str) -> tuple[str, str]:
    """(its screen, its title) of a function (its registry key) or a screen's action; a screen's (its title):
    (its parent, its title)."""
    if key in SCREENS:
        return SCREENS[key].parent, key
    if key in BOT_FUNCTIONS_BY_KEY:
        return BOT_FUNCTIONS_BY_KEY[key].category, BOT_FUNCTIONS_BY_KEY[key].title
    for title, screen in SCREENS.items():
        for button in screen.buttons:
            if button.action == key:
                return title, button.title
    raise KeyError(f"«{key}» нет в меню (modules/registry.py)")


def title(key: str) -> str:
    """A function's or a screen's action's title, as its button has it: for messages that name the button,
    so they can't outlive it renamed."""
    return _place(key)[1]


def menu_path(key: str) -> str:
    """Where a function (its registry key) or a screen's action is in the bot's menu, for messages that send
    the operator there: "📁 Проекты → проект → 🤖 Аккаунты → 📋 Список → 🩺 SpamBot"."""
    screen, name = _place(key)
    return " → ".join(CARD_IN_PATH if part == CARD else part for part in [*trail(screen), name])


# a function that runs on the workers, tapped in a project without any (the bot's pop-up)
NO_WORKERS = f"У проекта нет воркеров. Добавьте: {menu_path(ADD)}."


def by_category(category: str) -> list[BotFunction]:
    return [function for function in BOT_FUNCTIONS if function.category == category]


def missing_classes(functions: dict) -> list[str]:
    """Registry classnames with no discovered instance (would KeyError at runtime)."""
    return [function.classname for function in BOT_FUNCTIONS
            if function.classname and function.classname not in functions]
