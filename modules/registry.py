from dataclasses import dataclass

SAFE = "safe"
RISKY = "risky"


@dataclass(frozen=True)
class BotFunction:
    key: str        # callback id, used in FunctionCB
    classname: str  # TelethonFunction subclass name, as discovered from functions/
    title: str      # label shown in the menu
    risk: str       # SAFE or RISKY
    category: str   # menu category
    hint: str = ""  # one line under the title in the section message: what it does
    group: str = ""  # a subheading within its section's message


@dataclass(frozen=True)
class Button:
    """A screen's or a card's button that is not a screen of its own."""
    action: str       # a MenuAction value (bot/callbacks.py) on a screen, a ChoiceCB "prj_act" one on a card
    title: str = ""   # "" for a function's: its title in BOT_FUNCTIONS, ⚠️ as on the screens
    hint: str = ""    # one line next to its title on its screen: what it does
    function: str = ""  # the BOT_FUNCTIONS key it runs


@dataclass(frozen=True)
class Screen:
    title: str
    hint: str         # one line next to its button: in the /start message, or on its parent screen
    parent: str = ""  # the screen its ⬅️ Назад goes to (and its menu path); "" for a main-menu button
    buttons: tuple[Button, ...] = ()  # its actions, before its functions (BOT_FUNCTIONS of its category)
    keeps_form: bool = False  # only shows: opened, a form under way stays at its step (as /status does)


# The whole menu lives here: the bot's keyboards, its screens' texts, the CLI menu and the menu
# paths in messages (menu_path) are built from it, so a button can't lose its description.
# One tree of screens: each holds its actions, then its functions (under their subheadings), then
# the screens whose parent it is. The main menu (reply keyboard): the projects — all the work is a
# project's, on its own workers: its card (📁 Проект) holds its scrape and bases, its accounts and
# their activity —, the settings, the jobs running now. Every function has one place in it. How the
# bot shows a screen: bot/services/screens.py.
PROJECTS = "📁 Проекты"  # the projects: each one's card runs its scrape, the work on its bases, its accounts
CARD = "📁 Проект"  # the card of the chat's current project (bot/routers/projects.py), the parent of its screens
ACTIVITY = "💬 Активность"
ACCOUNTS = "🤖 Аккаунты"
PROFILE = "👤 Профиль"
SERVICE = "🛠 Обслуживание"
SETTINGS = "⚙️ Настройки"
TASKS = "📊 Задачи"
SCREENS = {screen.title: screen for screen in (  # in the CLI menu's order
    # its list and cards: bot/routers/projects.py
    Screen(PROJECTS, "ваши проекты: у каждого свои аккаунты, прокси, база и вся работа с ними"),
    Screen(CARD, "скрап и база проекта, его аккаунты и их активность", PROJECTS),
    Screen(ACCOUNTS, "воркеры проекта и личные аккаунты, прокси, проверка, профиль, обслуживание", CARD, buttons=(
        Button("list", "📋 Список", "имя, username и номер каждого воркера проекта, ограничения от @SpamBot, страны "
               "номеров; личные аккаунты; карточка каждого — код входа, удаление личного"),
        Button("add", "➕ Добавить", "по номеру и коду — воркер или личный (только для скрапа); воркер из tdata "
               "Telegram Desktop; или свободные аккаунты, ещё ни в одном проекте — отметками"),
        Button("proxy", "🌐 Прокси", "раздать прокси проекта его аккаунтам"),
        Button("move", "↪️ Перенести", "отметить воркеров проекта и перенести в другой проект или в свободные"),
    )),
    Screen(PROFILE, "имя, username, bio, фото, видимость", ACCOUNTS),
    Screen(SERVICE, "2FA, сброс чужих сессий, очистка диалогов", ACCOUNTS),
    Screen(ACTIVITY, "все воркеры проекта по одной цели: ЛС, комментарии, чаты, вступления, реакции, опросы, "
                     "инвайтинг, жалобы", CARD),
    Screen(SETTINGS, "задержки и автоответ для всех проектов (хранятся в config.toml)", buttons=(  # the CLI too
        Button("delay", "⏱ Задержка", "пауза между действиями воркера: рассылки, вступления, реакции, опросы, "
               "жалобы, инвайтинг, контакты"),
        Button("ppause", "⏳ Пауза профиля", "пауза между аккаунтами при смене имени, username, bio, фото и 2FA"),
        Button("autoreply", "📬 Автоответ", "ответ тем, кто ответил на рассылку в ЛС, и их сообщения сюда"),
    )),
    # its view: bot/routers/menu.py, as /status
    Screen(TASKS, "что сейчас выполняется во всех проектах: ход задач и их остановка", keeps_form=True),
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
    """The screen is a project's: it works on the chat's current project's workers."""
    return CARD in trail(title)


# the subheadings of the screens' functions
ONE_TARGET = "Написать — пишут все воркеры"
IN_CHATS = "Действия в чатах"
REPORTS = "Жалобы"

# Every function exposed through the bot, in menu order. All run on worker accounts; RISKY ones
# additionally require at least one worker (see WorkerPool). Scraping entries run
# through their own router (not WorkerPool) but still use a worker's session string.
BOT_FUNCTIONS = [
    # 📁 Проекты: a project's card (bot/routers/projects.py, functions/projects.py) runs these on the project's
    # files, after its scrape: a group's members as its base, the contacts first — a mailing then
    # writes to each person from the worker that added them — then the check of the scrape's files
    BotFunction("members", "MembersFunc", "👥 Участники чата", SAFE, PROJECTS,
                "весь список участников группы, включая молчащих; сразу база для рассылки"),
    BotFunction("addcontacts", "AddContactsFunc", "📇 В контакты", RISKY, PROJECTS,
                "добавляет людей из базы в контакты воркеров; рассылка потом пишет им с того же воркера"),
    BotFunction("pmmailing", "PmMailingFunc", "📨 ЛС по базе", RISKY, PROJECTS,
                "личные сообщения людям из скрапнутой базы или списка; помнит, кому уже писали, и делит людей "
                "между воркерами"),
    BotFunction("verify", "VerifyFunc", "✅ Верификация", SAFE, PROJECTS,
                "сверяет файл скрапа с каналом и находит пропущенные посты"),
    BotFunction("analysis", "ScraperAnalysisFunc", "📈 Анализ", SAFE, PROJECTS,
                "утилиты для файлов скрапа: ссылки на каналы, поиск по словам, просмотр"),
    # 💬 Активность: every worker writes to one target, then the actions in chats, then the reports
    BotFunction("pm", "PmBroadcastFunc", "✉️ ЛС человеку", RISKY, ACTIVITY,
                "все воркеры пишут одному человеку (по username или номеру)", ONE_TARGET),
    BotFunction("comments", "CommentsBroadcastFunc", "🗨 Комментарии", RISKY, ACTIVITY,
                "воркеры пишут комментарии под постом канала", ONE_TARGET),
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
    # 🤖 Аккаунты: the check most often needed, on the screen itself, after its actions
    BotFunction("status", "SpamBlockFunc", "🛡 Проверка статуса (SpamBot)", SAFE, ACCOUNTS,
                "спрашивает @SpamBot об ограничениях; бессрочно ограниченных исключает из рассылки, мёртвые сессии "
                "убирает"),
    # 🤖 Аккаунты → 👤 Профиль
    BotFunction("name", "ChangeNameFunc", "✏️ Имя", SAFE, PROFILE,
                "одно имя всем или случайное из списка"),
    BotFunction("username", "ChangeUsernameFunc", "🔤 Username", SAFE, PROFILE,
                "по основе со случайным числом (base24, base3071…) или из списка"),
    BotFunction("bio", "ChangeBioFunc", "📝 Bio", SAFE, PROFILE,
                "один текст «о себе» всем или случайный из списка"),
    BotFunction("photo", "ChangeProfilePhotoFunc", "🖼 Фото", SAFE, PROFILE,
                "одно фото всем или случайные из assets/photos"),
    BotFunction("clearchannel", "ClearPersonalChannelFunc", "📺 Убрать канал из профиля", SAFE, PROFILE,
                "снимает личный канал, закреплённый в профиле"),
    BotFunction("lastseen", "HideLastSeenFunc", "🙈 Скрыть последний визит", SAFE, PROFILE,
                "«был(а) недавно» вместо точного времени"),
    # 🤖 Аккаунты → 🛠 Обслуживание: the security, then the irreversible wipe
    BotFunction("2fa", "SetPasswordFunc", "🔒 Установить 2FA", SAFE, SERVICE,
                "ставит облачный пароль на все аккаунты"),
    BotFunction("terminate", "TerminateSessionsFunc", "📴 Сбросить чужие сессии", SAFE, SERVICE,
                "выкидывает из аккаунта все устройства, кроме бота"),
    BotFunction("clear", "ClearDialogsFunc", "🧹 Очистить диалоги", RISKY, SERVICE,
                "удаляет переписки у обеих сторон и выходит из всех каналов. Необратимо"),
]

BOT_FUNCTIONS_BY_KEY = {function.key: function for function in BOT_FUNCTIONS}


# 📁 Проекты → a project's card (bot/routers/projects.py, functions/projects.py), in the order of the
# work: collect, check the scrape, then the work on the base; the project itself one screen down.
# Each button's action is its value on the bot's card (ChoiceCB "prj_act"), its branch in the CLI's
PROJECT_CARD = (
    (Button("scrape", "▶️ Скрап проекта"), Button("topup", "🔄 Дособрать")),
    (Button("members", function="members"), Button("upload", "📥 Загрузить список")),
    (Button("verify", function="verify"), Button("analysis", function="analysis")),
    (Button("contacts", function="addcontacts"), Button("mail", function="pmmailing")),
    (Button("manage", "⚙️ Управление проектом"),),
)
PROJECT_MANAGE = (  # with hints: the card's own text is long, ⚙️'s lists what each does
    (Button("channels", "✏️ Каналы", "список каналов и групп проекта для скрапа"),
     Button("rename", "🏷 Переименовать", "новое имя проекта, его файлы переименуются вместе с ним")),
    (Button("clear", "🧹 Очистить базу", "участники групп или загруженный список уходят из проекта в "
            "<имя>_previous/, оттуда их можно вернуть"),
     Button("delete", "🗑 Удалить", "проект удаляется, файлы скрапа остаются; аккаунты сначала перенесите")),
)


def button_title(button: Button) -> str:
    """The title a button is shown with: a function's own, ⚠️ as on its section's screen."""
    if not button.function:
        return button.title
    function = BOT_FUNCTIONS_BY_KEY[button.function]
    return f"{risk_marker(function, by_category(function.category))}{function.title}"


def card_rows(rows, hidden=(), channels: bool = True) -> list[list[tuple[str, str]]]:
    """A card's (title, action) rows (PROJECT_CARD or PROJECT_MANAGE) as shown: without the `hidden`
    actions (🔄 with nothing to top up, 🧹 with nothing to clear), ✏️ Каналы as ➕ for a project without."""
    shown = []
    for row in rows:
        shown.append([])
        for button in row:
            if button.action in hidden:
                continue
            title = "➕ Добавить каналы" if button.action == "channels" and not channels else button_title(button)
            shown[-1].append((title, button.action))
    return [row for row in shown if row]


RISK_NOTE = "⚠️ — действия от имени воркеров: при частом использовании Telegram может ограничить аккаунт."


def risk_marker(function: BotFunction, section: list[BotFunction]) -> str:
    """⚠️ before a risky function's title — only in a section that has safe ones too: in an all-risky
    section the mark is once, on its title (screens.screen_text), and marking every button would say nothing."""
    mixed = any(f.risk != RISKY for f in section)
    return "⚠️ " if mixed and function.risk == RISKY else ""


def menu_path(key: str) -> str:
    """Where a function (its registry key) or a screen's action is in the bot's menu, for messages
    that send the operator there: "📁 Проекты → 📁 Проект → 🤖 Аккаунты → 🛡 Проверка статуса (SpamBot)"."""
    if key in BOT_FUNCTIONS_BY_KEY:
        function = BOT_FUNCTIONS_BY_KEY[key]
        on_card = any(button.function == key for row in PROJECT_CARD for button in row)
        parts = [*trail(CARD if on_card else function.category), function.title]
    else:
        parts = next([*trail(title), button.title] for title, screen in SCREENS.items()
                     for button in screen.buttons if button.action == key)
    return " → ".join(parts)


# The CLI menu: the same screens, titles and hints as the bot. Some of the bot's screens' actions
# (and 📁 Проекты, a main-menu button there) are CLI functions here.
CLI_EXTRAS = [
    BotFunction("projects", "ProjectsFunc", "📁 Проекты", SAFE, PROJECTS,
                "ваши проекты: у каждого свои аккаунты и прокси; скрап и дособор, рассылка, контакты, верификация, "
                "анализ, активность воркеров"),
    BotFunction("accounts", "AccountsFunc", "📋 Список", SAFE, ACCOUNTS,
                "имя, username, номер, прокси и статус каждого воркера проекта, ограничения от @SpamBot, страны "
                "номеров; личные аккаунты; карточка каждого — код входа, удаление личного"),
    BotFunction("proxies", "SetProxiesFunc", "🌐 Прокси", SAFE, ACCOUNTS,
                "раздать прокси из файла аккаунтам проекта"),
    BotFunction("phone", "AddByPhoneFunc", "➕ Добавить", SAFE, ACCOUNTS,
                "по номеру и коду — воркер или личный (только для скрапа); воркеры из папок tdata_import/ "
                "(Telegram Desktop); или свободные аккаунты, ещё ни в одном проекте — номерами"),
    BotFunction("move", "MoveWorkersFunc", "↪️ Перенести", SAFE, ACCOUNTS,
                "воркеры проекта по номерам — в другой проект или в свободные"),
    BotFunction("settings", "SettingsFunc", "⚙️ Настройки", SAFE, SETTINGS,
                "задержка и пауза профиля для всех проектов (хранятся в config.toml)"),
]


# run from another item only, never a menu item of their own: a project's card (functions/projects.py),
# ➕ Добавить (functions/sign_in.py), an account's card (functions/accounts.py)
NOT_MENU_ITEMS = ("ScrapeFunc", *(BOT_FUNCTIONS_BY_KEY[button.function].classname for row in PROJECT_CARD
                                   for button in row if button.function), "TdataImportFunc", "LoginCodesFunc")


def cli_menu(project: bool = False) -> list[tuple[str, list[BotFunction]]]:
    """[(screen, functions)] in the bot's order: a screen's actions (CLI_EXTRAS) before its functions;
    a project card's items are the card's only. `project`: a project's screens (in_project), for its
    card; else the main menu's."""
    every = [f for f in CLI_EXTRAS + BOT_FUNCTIONS if f.classname not in NOT_MENU_ITEMS]
    return [(title, [f for f in every if f.category == title]) for title in SCREENS if in_project(title) == project]


def by_category(category: str) -> list[BotFunction]:
    return [function for function in BOT_FUNCTIONS if function.category == category]


def missing_classes(functions: dict) -> list[str]:
    """Registry classnames with no discovered instance (would KeyError at runtime)."""
    return [function.classname for function in BOT_FUNCTIONS if function.classname not in functions]
