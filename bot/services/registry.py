import html
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


@dataclass(frozen=True)
class ScreenButton:
    title: str
    hint: str        # one line next to the title on its screen: what it does
    action: str = ""  # a MenuAction value (bot/callbacks.py); "" for a group of functions: its title is their category


# The whole menu lives here: the bot's keyboards, its screens' texts, the CLI menu and the menu
# paths in messages (menu_path) are built from it, so a button can't lose its description.
# Main menu (reply keyboard): the projects on top, the sections of functions, then the two hub screens.
PROJECTS = "📁 Проекты"  # the audiences: each project's card runs the scrape and the work on its bases
SECTIONS = ("📣 Рассылки", "🔍 Сбор данных", "💬 Активность")
WORKERS = "🤖 Воркеры"
SETTINGS = "⚙️ Настройки"
MAIN_MENU_HINTS = {  # the /start message: what each main-menu button holds
    PROJECTS: "каналы, сбор и дособор, базы — и рассылка, контакты, проверка, анализ по ним",
    "📣 Рассылки": "контакты, в личку, в комментарии и в чаты",
    "🔍 Сбор данных": "скрап, участники чата, проверка и анализ — разово, без проекта",
    "💬 Активность": "вступления, реакции, опросы, репорты, инвайтинг",
    WORKERS: "аккаунты, прокси, профиль, безопасность, проверка",
    SETTINGS: "задержки и автоответ (хранятся в config.toml)",
}
SCREENS = {
    WORKERS: [  # the accounts themselves, then the groups of functions run on them
        ScreenButton("📋 Список аккаунтов", "имя, username и номер каждого воркера, ограничения от @SpamBot; "
                     "личные аккаунты (и их удаление)", "list"),
        ScreenButton("📲 Добавить по номеру", "войти по номеру и коду: воркер или личный аккаунт (только для скрапа)",
                     "phone"),
        ScreenButton("📥 Загрузить tdata", "добавить аккаунт из Telegram Desktop", "tdata"),
        ScreenButton("🔑 Код входа", "коды от Telegram, пришедшие воркеру или личному аккаунту "
                     "(для входа с другого устройства)", "codes"),
        ScreenButton("🌐 Прокси", "раздать прокси всем аккаунтам", "proxy"),
        ScreenButton("👤 Профиль", "имя, username, bio, фото, видимость"),
        ScreenButton("🔐 Безопасность", "2FA и сброс чужих сессий"),
        ScreenButton("🩺 Проверка и обслуживание", "ограничения от @SpamBot, страны номеров, очистка диалогов"),
    ],
    SETTINGS: [  # written to config.toml, effective at once (the CLI reads them too)
        ScreenButton("⏱ Задержка", "пауза между действиями воркера: рассылки, вступления, реакции, опросы, "
                     "жалобы, инвайтинг, контакты", "delay"),
        ScreenButton("⏳ Пауза профиля", "пауза между аккаунтами при смене имени, username, bio, фото и 2FA",
                     "ppause"),
        ScreenButton("📬 Автоответ", "ответ тем, кто ответил на рассылку в ЛС, и их сообщения сюда", "autoreply"),
    ],
}
# the groups of functions on the workers screen (each one a category of BOT_FUNCTIONS)
WORKER_GROUPS = tuple(button.title for button in SCREENS[WORKERS] if not button.action)

# Every function exposed through the bot, in menu order. All run on worker accounts; RISKY ones
# additionally require at least one worker (see WorkerPool). Scraping entries run
# through their own router (not WorkerPool) but still use a worker's session string.
BOT_FUNCTIONS = [
    # 📁 Проекты (its own main-menu button)
    BotFunction("projects", "ProjectsFunc", "📁 Проекты", SAFE, PROJECTS,
                "сохранённые наборы каналов и вся работа с их базами: скрап и дособор, рассылка, контакты, "
                "верификация, анализ"),
    # 📣 Рассылки: the contacts first — a mailing then writes to each person from the worker that added them
    BotFunction("addcontacts", "AddContactsFunc", "📇 Добавить в контакты", RISKY, "📣 Рассылки",
                "добавляет людей из базы в контакты воркеров; рассылка потом пишет им с того же воркера"),
    BotFunction("pmmailing", "PmMailingFunc", "📨 ЛС по базе (со статой)", RISKY, "📣 Рассылки",
                "личные сообщения людям из скрапнутой базы или списка; помнит, кому уже писали, и делит людей между воркерами"),
    BotFunction("pm", "PmBroadcastFunc", "✉️ ЛС одному получателю", RISKY, "📣 Рассылки",
                "все воркеры пишут одному человеку (по username или номеру)"),
    BotFunction("comments", "CommentsBroadcastFunc", "🗨 В комментарии к посту", RISKY, "📣 Рассылки",
                "воркеры пишут комментарии под постом канала"),
    BotFunction("instant", "InstantBroadcastFunc", "⚡ В чат — сразу", RISKY, "📣 Рассылки",
                "воркеры сразу отправляют сообщения в чат или группу"),
    BotFunction("chat", "BroadcastChatFunc", "🔔 В чат — по триггеру", RISKY, "📣 Рассылки",
                "воркеры ждут кодовое слово в чате и после него начинают писать"),
    # 🔍 Сбор данных: collect → check
    BotFunction("scrape", "ScrapeFunc", "📡 Скрап канала/группы", SAFE, "🔍 Сбор данных",
                "скачивает сообщения, их авторов, комментаторов и реакции из каналов и групп; "
                "собирает базу людей для рассылки"),
    BotFunction("members", "MembersFunc", "👥 Участники чата", SAFE, "🔍 Сбор данных",
                "весь список участников группы, включая молчащих; сразу база для рассылки"),
    BotFunction("verify", "VerifyFunc", "✅ Верификация скрапа", SAFE, "🔍 Сбор данных",
                "сверяет файл скрапа с каналом и находит пропущенные посты"),
    BotFunction("analysis", "ScraperAnalysisFunc", "📈 Анализ данных", SAFE, "🔍 Сбор данных",
                "утилиты для файлов скрапа: ссылки на каналы, поиск по словам, просмотр"),
    # 💬 Активность: actions in other people's chats
    BotFunction("join", "JoinerFunc", "🚪 Вступить в чат", RISKY, "💬 Активность",
                "воркеры вступают в канал/группу, умеют проходить капчу"),
    BotFunction("reactions", "ReactionsFunc", "❤️ Реакции на пост", RISKY, "💬 Активность",
                "каждый воркер ставит реакцию на сообщение или пост"),
    BotFunction("poll", "PollVoteFunc", "🗳 Голосование в опросе", RISKY, "💬 Активность",
                "воркеры голосуют за выбранный вариант"),
    BotFunction("report", "ReportFunc", "🚩 Репорт сообщения/поста", RISKY, "💬 Активность",
                "жалоба на посты; путь жалобы выбираете один раз, остальные воркеры повторяют"),
    BotFunction("reportuser", "ReportUserFunc", "🚫 Репорт пользователя", RISKY, "💬 Активность",
                "жалоба на аккаунт от всех воркеров"),
    BotFunction("invite", "InvitingFunc", "🤝 Инвайтинг из чата", RISKY, "💬 Активность",
                "приглашает участников одного чата в другой"),
    # 🤖 Воркеры → 👤 Профиль
    BotFunction("name", "ChangeNameFunc", "✏️ Сменить имя", SAFE, "👤 Профиль",
                "одно имя всем или случайное из списка"),
    BotFunction("username", "ChangeUsernameFunc", "🔤 Сменить username", SAFE, "👤 Профиль",
                "по основе со случайным числом (base24, base3071…) или из списка"),
    BotFunction("bio", "ChangeBioFunc", "📝 Сменить bio", SAFE, "👤 Профиль",
                "один текст «о себе» всем или случайный из списка"),
    BotFunction("photo", "ChangeProfilePhotoFunc", "🖼 Сменить фото", SAFE, "👤 Профиль",
                "одно фото всем или случайные из assets/photos"),
    BotFunction("clearchannel", "ClearPersonalChannelFunc", "📺 Убрать канал из профиля", SAFE, "👤 Профиль",
                "снимает личный канал, закреплённый в профиле"),
    BotFunction("lastseen", "HideLastSeenFunc", "🙈 Скрыть последний визит", SAFE, "👤 Профиль",
                "«был(а) недавно» вместо точного времени"),
    # 🤖 Воркеры → 🔐 Безопасность
    BotFunction("2fa", "SetPasswordFunc", "🔒 Установить 2FA", SAFE, "🔐 Безопасность",
                "ставит облачный пароль на все аккаунты"),
    BotFunction("terminate", "TerminateSessionsFunc", "📴 Сбросить чужие сессии", SAFE, "🔐 Безопасность",
                "выкидывает из аккаунта все устройства, кроме бота"),
    # 🤖 Воркеры → 🩺 Проверка и обслуживание
    BotFunction("status", "SpamBlockFunc", "🛡 Проверка статуса (SpamBot)", SAFE, "🩺 Проверка и обслуживание",
                "спрашивает @SpamBot об ограничениях; бессрочно ограниченных исключает из рассылки, мёртвые сессии убирает"),
    BotFunction("stats", "PhoneNumbersStatsFunc", "🌍 Статистика по номерам", SAFE, "🩺 Проверка и обслуживание",
                "сколько аккаунтов из каких стран (по коду номера)"),
    BotFunction("clear", "ClearDialogsFunc", "🧹 Очистить диалоги", RISKY, "🩺 Проверка и обслуживание",
                "удаляет переписки у обеих сторон и выходит из всех каналов. Необратимо"),
]

BOT_FUNCTIONS_BY_KEY = {function.key: function for function in BOT_FUNCTIONS}


RISK_NOTE = "⚠️ — действия от имени воркеров в чужих чатах: при частом использовании Telegram может ограничить аккаунт."


def section_text(category: str) -> str:
    """The section message: each function's title and what it does (HTML)."""
    functions = by_category(category)
    lines = [f"<b>{html.escape(category)}</b>", ""]
    for function in functions:
        marker = "⚠️ " if function.risk == RISKY else ""
        lines.append(f"{marker}<b>{html.escape(function.title)}</b> — {html.escape(function.hint)}")
    if any(function.risk == RISKY for function in functions):
        lines += ["", f"<i>{RISK_NOTE}</i>"]
    return "\n".join(lines)


def screen_text(screen: str) -> str:
    """A hub screen's buttons, each with what it does (HTML), as section_text for a section."""
    return "\n".join(f"<b>{html.escape(button.title)}</b> — {html.escape(button.hint)}" for button in SCREENS[screen])


def menu_path(key: str) -> str:
    """Where a function (its registry key) or a hub screen's button (its action) is in the bot's
    menu, for messages that send the operator there: "🤖 Воркеры → 🩺 … → 🛡 Проверка статуса (SpamBot)"."""
    if key in BOT_FUNCTIONS_BY_KEY:
        function = BOT_FUNCTIONS_BY_KEY[key]
        parts = [*([WORKERS] if function.category in WORKER_GROUPS else []), function.category, function.title]
        return " → ".join(dict.fromkeys(parts))  # the projects' button is its own category
    return next(f"{screen} → {button.title}" for screen, buttons in SCREENS.items()
                for button in buttons if button.action == key)


# The CLI menu: the same sections, titles and hints as the bot. Some of the bot's
# workers-screen buttons are CLI functions there.
CLI_WORKERS = WORKERS
CLI_EXTRAS = [
    BotFunction("accounts", "AccountsFunc", "📋 Список аккаунтов", SAFE, CLI_WORKERS,
                "имя, username, номер, прокси и статус каждого воркера, ограничения от @SpamBot"),
    BotFunction("proxies", "SetProxiesFunc", "🌐 Прокси", SAFE, CLI_WORKERS,
                "раздать прокси из файла всем аккаунтам"),
    BotFunction("phone", "AddByPhoneFunc", "📲 Добавить по номеру", SAFE, CLI_WORKERS,
                "войти по номеру и коду: воркер или личный аккаунт (только для скрапа)"),
    BotFunction("tdata", "TdataImportFunc", "📥 Загрузить tdata", SAFE, CLI_WORKERS,
                "добавить аккаунты из папок tdata_import/ (Telegram Desktop), без перезапуска"),
    BotFunction("codes", "LoginCodesFunc", "🔑 Код входа", SAFE, CLI_WORKERS,
                "коды от Telegram, пришедшие воркеру или личному аккаунту (для входа с другого устройства)"),
    BotFunction("rmpersonal", "RemovePersonalFunc", "🗑 Убрать личный аккаунт", SAFE, CLI_WORKERS,
                "завершить сессию teleharvester на личном аккаунте и удалить его файл"),
    BotFunction("settings", "SettingsFunc", "⚙️ Настройки", SAFE, SETTINGS,
                "задержка и пауза профиля (хранятся в config.toml)"),
]


def cli_menu() -> list[tuple[str, list[BotFunction]]]:
    """[(section, functions)] in the bot's order, the workers' own items first."""
    every = BOT_FUNCTIONS + CLI_EXTRAS
    order = [PROJECTS, *SECTIONS, CLI_WORKERS, *WORKER_GROUPS, SETTINGS]
    return [(section, [f for f in every if f.category == section]) for section in order]


def by_category(category: str) -> list[BotFunction]:
    return [function for function in BOT_FUNCTIONS if function.category == category]


def missing_classes(functions: dict) -> list[str]:
    """Registry classnames with no discovered instance (would KeyError at runtime)."""
    return [function.classname for function in BOT_FUNCTIONS if function.classname not in functions]
