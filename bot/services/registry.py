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


# Main-menu sections (reply keyboard) and the subgroups inside "🤖 Воркеры".
SECTIONS = ("📣 Рассылки", "💬 Активность", "🎯 Аудитория")
WORKER_GROUPS = ("👤 Профиль", "🔐 Безопасность", "🩺 Проверка и статистика")

# Every function exposed through the bot, in menu order. All run on worker accounts; RISKY ones
# additionally require at least one worker (see WorkerPool). Scraping entries run
# through their own router (not WorkerPool) but still use a worker's session string.
BOT_FUNCTIONS = [
    # 📣 Рассылки
    BotFunction("pmmailing", "PmMailingFunc", "ЛС по базе (со статой)", RISKY, "📣 Рассылки",
                "личные сообщения людям из скрапнутой базы или списка; помнит, кому уже писали, и делит людей между воркерами"),
    BotFunction("pm", "PmBroadcastFunc", "ЛС одному получателю", RISKY, "📣 Рассылки",
                "все воркеры пишут одному человеку (по username или номеру)"),
    BotFunction("comments", "CommentsBroadcastFunc", "В комментарии к посту", RISKY, "📣 Рассылки",
                "воркеры пишут комментарии под постом канала"),
    BotFunction("instant", "InstantBroadcastFunc", "В чат — сразу", RISKY, "📣 Рассылки",
                "воркеры сразу отправляют сообщения в чат или группу"),
    BotFunction("chat", "BroadcastChatFunc", "В чат — по триггеру", RISKY, "📣 Рассылки",
                "воркеры ждут кодовое слово в чате и после него начинают писать"),
    # 💬 Активность
    BotFunction("join", "JoinerFunc", "Вступить в чат", RISKY, "💬 Активность",
                "воркеры вступают в канал/группу, умеют проходить капчу"),
    BotFunction("reactions", "ReactionsFunc", "Реакции на пост", RISKY, "💬 Активность",
                "каждый воркер ставит реакцию на сообщение или пост"),
    BotFunction("poll", "PollVoteFunc", "Голосование в опросе", RISKY, "💬 Активность",
                "воркеры голосуют за выбранный вариант"),
    BotFunction("report", "ReportFunc", "Репорт сообщения/поста", RISKY, "💬 Активность",
                "жалоба на посты; путь жалобы выбираете один раз, остальные воркеры повторяют"),
    BotFunction("reportuser", "ReportUserFunc", "Репорт пользователя", RISKY, "💬 Активность",
                "жалоба на аккаунт от всех воркеров"),
    # 🎯 Аудитория: collect → check → use
    BotFunction("scrape", "ScrapeFunc", "Скрап канала/группы", SAFE, "🎯 Аудитория",
                "скачивает сообщения, их авторов, комментаторов и реакции из каналов и групп; "
                "собирает базу людей для рассылки"),
    BotFunction("members", "MembersFunc", "Участники чата", SAFE, "🎯 Аудитория",
                "весь список участников группы, включая молчащих; сразу база для рассылки"),
    BotFunction("verify", "VerifyFunc", "Верификация скрапа", SAFE, "🎯 Аудитория",
                "сверяет файл скрапа с каналом и находит пропущенные посты"),
    BotFunction("analysis", "ScraperAnalysisFunc", "Анализ данных", SAFE, "🎯 Аудитория",
                "утилиты для файлов скрапа: ссылки на каналы, поиск по словам, просмотр"),
    BotFunction("invite", "InvitingFunc", "Инвайтинг из чата", RISKY, "🎯 Аудитория",
                "приглашает участников одного чата в другой"),
    BotFunction("addcontacts", "AddContactsFunc", "Добавить в контакты (.parquet)", RISKY, "🎯 Аудитория",
                "добавляет людей из базы в контакты воркеров; рассылка потом пишет им с того же воркера"),
    # 🤖 Воркеры → 👤 Профиль
    BotFunction("name", "ChangeNameFunc", "Сменить имя", SAFE, "👤 Профиль",
                "одно имя всем или случайное из списка"),
    BotFunction("username", "ChangeUsernameFunc", "Сменить username", SAFE, "👤 Профиль",
                "по основе со случайным числом (base24, base3071…) или из списка"),
    BotFunction("bio", "ChangeBioFunc", "Сменить bio", SAFE, "👤 Профиль",
                "один текст «о себе» всем или случайный из списка"),
    BotFunction("photo", "ChangeProfilePhotoFunc", "Сменить фото", SAFE, "👤 Профиль",
                "одно фото всем или случайные из assets/photos"),
    BotFunction("clearchannel", "ClearPersonalChannelFunc", "Убрать канал из профиля", SAFE, "👤 Профиль",
                "снимает личный канал, закреплённый в профиле"),
    BotFunction("lastseen", "HideLastSeenFunc", "Скрыть последний визит", SAFE, "👤 Профиль",
                "«был(а) недавно» вместо точного времени"),
    # 🤖 Воркеры → 🔐 Безопасность
    BotFunction("2fa", "SetPasswordFunc", "Установить 2FA", SAFE, "🔐 Безопасность",
                "ставит облачный пароль на все аккаунты"),
    BotFunction("terminate", "TerminateSessionsFunc", "Сбросить чужие сессии", SAFE, "🔐 Безопасность",
                "выкидывает из аккаунта все устройства, кроме бота"),
    # 🤖 Воркеры → 🩺 Проверка и статистика
    BotFunction("status", "SpamBlockFunc", "Проверка статуса (SpamBot)", SAFE, "🩺 Проверка и статистика",
                "спрашивает @SpamBot об ограничениях; бессрочно ограниченных исключает из рассылки, мёртвые сессии убирает"),
    BotFunction("stats", "PhoneNumbersStatsFunc", "Статистика по номерам", SAFE, "🩺 Проверка и статистика",
                "сколько аккаунтов из каких стран (по коду номера)"),
    BotFunction("clear", "ClearDialogsFunc", "Очистить диалоги", RISKY, "🩺 Проверка и статистика",
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


# The CLI menu: the same sections, titles and hints as the bot. Some of the bot's
# workers-screen buttons are CLI functions there.
CLI_WORKERS = "🤖 Воркеры"
CLI_EXTRAS = [
    BotFunction("accounts", "AccountsFunc", "Список аккаунтов", SAFE, CLI_WORKERS,
                "имя, username, номер, прокси и статус каждого воркера, ограничения от @SpamBot"),
    BotFunction("proxies", "SetProxiesFunc", "Прокси", SAFE, CLI_WORKERS,
                "раздать прокси из файла всем аккаунтам"),
    BotFunction("phone", "AddByPhoneFunc", "Добавить по номеру", SAFE, CLI_WORKERS,
                "войти по номеру и коду: воркер или личный аккаунт (только для скрапа)"),
    BotFunction("codes", "LoginCodesFunc", "Код входа", SAFE, CLI_WORKERS,
                "коды от Telegram, пришедшие воркеру или личному аккаунту (для входа с другого устройства)"),
    BotFunction("rmpersonal", "RemovePersonalFunc", "Убрать личный аккаунт", SAFE, CLI_WORKERS,
                "завершить сессию teleharvester на личном аккаунте и удалить его файл"),
]


def cli_menu() -> list[tuple[str, list[BotFunction]]]:
    """[(section, functions)] in the bot's order, the workers' own items first."""
    every = BOT_FUNCTIONS + CLI_EXTRAS
    order = [*SECTIONS, CLI_WORKERS, *WORKER_GROUPS]
    return [(section, [f for f in every if f.category == section]) for section in order]


def by_category(category: str) -> list[BotFunction]:
    return [function for function in BOT_FUNCTIONS if function.category == category]


def missing_classes(functions: dict) -> list[str]:
    """Registry classnames with no discovered instance (would KeyError at runtime)."""
    return [function.classname for function in BOT_FUNCTIONS if function.classname not in functions]
