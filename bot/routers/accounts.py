from aiogram import F, Router
from aiogram.types import Message

from bot.services.delegation import WorkerPool

router = Router()


@router.message(F.text == "👥 Аккаунты")
async def accounts(message: Message, pool: WorkerPool):
    count = pool.count()

    text = (
        "👥 <b>Аккаунты</b>\n\n"
        "🛡 Хост (этот бот): рискованные действия заблокированы.\n"
        f"🤖 Воркеров: <b>{count}</b>\n\n"
    )

    if count == 0:
        text += "Добавьте сессии в <code>sessions/</code>, чтобы запускать задачи."
    else:
        text += "Все задачи выполняются на воркерах."

    await message.answer(text, parse_mode="HTML")
