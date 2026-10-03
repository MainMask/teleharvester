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


# Every function exposed through the bot. All run on worker accounts; RISKY ones
# additionally require at least one worker (see WorkerPool). Scraping entries run
# through their own router (not WorkerPool) but still use a worker's session string.
BOT_FUNCTIONS = [
    # 📣 Рассылки
    BotFunction("pmmailing", "PmMailingFunc", "Рассылка в ЛС (со статой)", RISKY, "📣 Рассылки"),
    BotFunction("pm", "PmBroadcastFunc", "Рассылка в ЛС (один получатель)", RISKY, "📣 Рассылки"),
    BotFunction("comments", "CommentsBroadcastFunc", "Рассылка в комментарии", RISKY, "📣 Рассылки"),
    BotFunction("instant", "InstantBroadcastFunc", "Мгновенная рассылка", RISKY, "📣 Рассылки"),
    BotFunction("chat", "BroadcastChatFunc", "Рассылка в чат (триггер)", RISKY, "📣 Рассылки"),
    # 👤 Профиль
    BotFunction("name", "ChangeNameFunc", "Сменить имя", SAFE, "👤 Профиль"),
    BotFunction("username", "ChangeUsernameFunc", "Сменить username", SAFE, "👤 Профиль"),
    BotFunction("bio", "ChangeBioFunc", "Сменить bio", SAFE, "👤 Профиль"),
    BotFunction("photo", "ChangeProfilePhotoFunc", "Сменить фото", SAFE, "👤 Профиль"),
    BotFunction("2fa", "SetPasswordFunc", "Установить 2FA", SAFE, "👤 Профиль"),
    # 🎯 Аудитория
    BotFunction("invite", "InvitingFunc", "Инвайтинг из чата", RISKY, "🎯 Аудитория"),
    BotFunction("addcontacts", "AddContactsFunc", "Добавить в контакты (.parquet)", RISKY, "🎯 Аудитория"),
    # ⚡ Активность
    BotFunction("join", "JoinerFunc", "Вступление в чат", RISKY, "⚡ Активность"),
    BotFunction("reactions", "ReactionsFunc", "Реакции на пост", RISKY, "⚡ Активность"),
    BotFunction("poll", "PollVoteFunc", "Голосование в опросе", RISKY, "⚡ Активность"),
    # 🛡 Модерация
    BotFunction("report", "ReportFunc", "Репорт сообщения/поста", RISKY, "🛡 Модерация"),
    BotFunction("reportuser", "ReportUserFunc", "Репорт пользователя", RISKY, "🛡 Модерация"),
    # 🧹 Сервис
    BotFunction("status", "SpamBlockFunc", "Проверка статуса", SAFE, "🧹 Сервис"),
    BotFunction("stats", "PhoneNumbersStatsFunc", "Статистика по номерам", SAFE, "🧹 Сервис"),
    BotFunction("terminate", "TerminateSessionsFunc", "Сбросить чужие сессии", SAFE, "🧹 Сервис"),
    BotFunction("clear", "ClearDialogsFunc", "Очистить диалоги", RISKY, "🧹 Сервис"),
    # 🔎 Скрапинг
    BotFunction("scrape", "ScrapeFunc", "Скрап канала/группы", SAFE, "🔎 Скрапинг"),
    BotFunction("verify", "VerifyFunc", "Верификация скрапа", SAFE, "🔎 Скрапинг"),
    BotFunction("analysis", "ScraperAnalysisFunc", "Анализ данных", SAFE, "🔎 Скрапинг"),
]

BOT_FUNCTIONS_BY_KEY = {function.key: function for function in BOT_FUNCTIONS}


def categories() -> list[str]:
    """Category names in first-seen order."""
    seen = []
    for function in BOT_FUNCTIONS:
        if function.category not in seen:
            seen.append(function.category)
    return seen


def by_category(category: str) -> list[BotFunction]:
    return [function for function in BOT_FUNCTIONS if function.category == category]


def missing_classes(functions: dict) -> list[str]:
    """Registry classnames with no discovered instance (would KeyError at runtime)."""
    return [function.classname for function in BOT_FUNCTIONS if function.classname not in functions]
