"""💬 Активность: the workers writing in a chat — 🗨 Комментарии under a post, ⚡ Чат — сразу, 🔔 Чат — по слову.
One chain of steps (FSM data "flow" says which): what to send and the mentions (the chats'), a sticker set,
the chat's link or the trigger, the messages count, then the message itself."""
import asyncio

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import ChoiceCB, FunctionCB
from bot.keyboards.common import cancel_kb, choice_kb, yes_no_kb
from bot.routers._common import (SEND_MESSAGE_PROMPT, STALE_STEP, back_answer, build_content, busy_step, left_flow,
                                 require_post_link, require_text, resolve, start_form)
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from bot.states import ChatPost
from functions.broadcast import MENTION_ADMINS, MENTION_USERS
from modules.registry import ACTIVITY
from modules.settings import Settings, count_text

router = Router()

MODE_OPTIONS = [("📝 Текст", "0"), ("🖼 Медиа", "2"), ("💬 Ответом на сообщение", "3"), ("🎭 Стикеры", "4")]
INSTANT_MODES = [("📝 Текст", "0"), ("🖼 Медиа", "2"), ("🎭 Стикеры", "4")]  # reply needs a trigger
MMODE_OPTIONS = [(f"👮 {MENTION_ADMINS} случайных админа", "admins"),
                 (f"👤 {MENTION_USERS} случайных участников", "users")]
MENTION_ASK = "Добавлять в сообщения скрытые упоминания участников чата? Упомянутым придёт уведомление."
MENTION_WHO = "Кого упоминать в каждом сообщении?"
STICKERS = 4  # the mode that asks for a sticker set

# what each flow asks first: its modes (instant, chat), or the post's link (comments)
_MODES = {
    "instant": ("⚡ Чат — сразу: что отправлять?", INSTANT_MODES),
    "chat": ("🔔 Чат — по слову: что отправлять?\n💬 Ответом на сообщение — отправьте триггер ответом на нужное "
             "сообщение: воркеры ответят на него.", MODE_OPTIONS),
}
# what its run needs, from the steps before the message: a stale message step has some missing
_NEEDS = {"comments": ("link",), "instant": ("choice", "link"), "chat": ("choice", "trigger")}


@router.callback_query(FunctionCB.filter(F.key.in_({"comments", "instant", "chat"})))
async def start(callback: CallbackQuery, callback_data: FunctionCB, state: FSMContext, pool: WorkerPool):
    flow = dict(flow=callback_data.key)
    if callback_data.key == "comments":
        await start_form(callback, state, ChatPost.link, "🗨 Комментарии — пришлите ссылку на пост:", pool, data=flow)
        return
    ask, modes = _MODES[callback_data.key]
    await start_form(callback, state, ChatPost.mode, ask, pool, choice_kb("bc_mode", modes), data=flow)


@router.callback_query(ChoiceCB.filter(F.scope == "bc_mode"))
async def mode_pick(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext):
    if await left_flow(callback, state, ChatPost.mode):
        return
    await state.update_data(choice=int(callback_data.value))
    await callback.message.edit_text(MENTION_ASK, reply_markup=cancel_kb(ChatPost.mode, yes_no_kb("bc_mention")))
    await callback.answer()


@router.callback_query(ChoiceCB.filter(F.scope == "bc_mention"))
async def mention_pick(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext, settings: Settings):
    if await left_flow(callback, state, ChatPost.mode):
        return
    await state.update_data(mention_all=callback_data.value == "yes")
    await callback.answer()
    if callback_data.value == "yes":
        await callback.message.edit_text(MENTION_WHO,
                                         reply_markup=cancel_kb(ChatPost.mode, choice_kb("bc_mmode", MMODE_OPTIONS)))
    else:
        await _after_mentions(callback.message.edit_text, state, settings)


@router.callback_query(ChoiceCB.filter(F.scope == "bc_mmode"))
async def mmode_pick(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext, settings: Settings):
    if await left_flow(callback, state, ChatPost.mode):
        return
    await state.update_data(mention_mode=callback_data.value)
    await callback.answer()
    await _after_mentions(callback.message.edit_text, state, settings)


# The steps after a choice by button ask in its message's place (`say`: its edit_text), after a typed
# answer in a new message (`say`: its answer).

async def _after_mentions(say, state: FSMContext, settings: Settings):
    if (await state.get_data()).get("choice") == STICKERS:
        await state.set_state(ChatPost.sticker)
        await say("Пришлите ссылку на стикерсет:", reply_markup=cancel_kb(ChatPost.sticker))
    else:
        await _after_sticker(say, state, settings)


@router.message(ChatPost.sticker)
async def sticker_input(message: Message, state: FSMContext, settings: Settings):
    sticker_set = await require_text(message)
    if sticker_set is None:
        return
    await state.update_data(sticker_set=sticker_set)
    await _after_sticker(message.answer, state, settings)


async def _after_sticker(say, state: FSMContext, settings: Settings):
    """The chat's link (instant) or the trigger (chat)."""
    if (await state.get_data()).get("flow") == "chat":
        await _ask_trigger(say, state, settings)
        return
    await state.set_state(ChatPost.link)
    await say("Пришлите ссылку на чат или канал:", reply_markup=cancel_kb(ChatPost.link))


@router.message(ChatPost.link)
async def link_input(message: Message, state: FSMContext, settings: Settings):
    comments = (await state.get_data()).get("flow") == "comments"
    link = await (require_post_link(message) if comments else require_text(message))
    if link is None:
        return
    await state.update_data(link=link)
    await _ask_count(message.answer, state, settings)


# ================= The trigger and the messages count =================
# Chosen per run, with the current config value one tap away; written to config.toml only
# when the job really starts (_save_options), so a run refused for a busy slot changes nothing
# and a running job — which reads settings live — is never switched mid-way.

async def _ask_trigger(say, state: FSMContext, settings: Settings):
    await state.set_state(ChatPost.trigger)
    if not settings.trigger:  # nothing to keep: an empty trigger would fire on any caption-less media
        await say("Триггер — текст, после которого воркеры начнут писать в чат. Пришлите его:",
                  reply_markup=cancel_kb(ChatPost.trigger))
        return
    await say(
        f"Триггер — текст, после которого воркеры начнут писать в чат. Сейчас: «{settings.trigger}».\n"
        "Оставьте его или пришлите новый:",
        reply_markup=cancel_kb(ChatPost.trigger,
                               choice_kb("bc_trigger", [(f"✅ Оставить «{settings.trigger[:40]}»", "keep")])),
    )


async def _ask_count(say, state: FSMContext, settings: Settings):
    await state.set_state(ChatPost.count)
    await say(
        f"Сколько сообщений отправит каждый воркер? Сейчас: {count_text(settings.messages_count)}.\n"
        "Пришлите число, оставьте текущее или «♾ Без лимита» — до ⏹:",
        reply_markup=cancel_kb(ChatPost.count, choice_kb(
            "bc_count", [*([(f"✅ Оставить: {count_text(settings.messages_count)}", "keep")]
                           if settings.messages_count else []), ("♾ Без лимита", "0")])),
    )


async def _ask_message(say, state: FSMContext):
    await state.set_state(ChatPost.message)
    await say(SEND_MESSAGE_PROMPT, reply_markup=cancel_kb(ChatPost.message))


@router.callback_query(ChatPost.trigger, ChoiceCB.filter(F.scope == "bc_trigger"))
async def trigger_keep(callback: CallbackQuery, state: FSMContext, settings: Settings):
    await callback.answer()
    if not settings.trigger:
        return
    await state.update_data(trigger=settings.trigger)
    await _ask_count(callback.message.edit_text, state, settings)


@router.message(ChatPost.trigger)
async def trigger_input(message: Message, state: FSMContext, settings: Settings):
    trigger = await require_text(message)
    if trigger is None:
        return
    await state.update_data(trigger=trigger)
    await _ask_count(message.answer, state, settings)


@router.callback_query(ChatPost.count, ChoiceCB.filter(F.scope == "bc_count"))
async def count_keep(callback: CallbackQuery, callback_data: ChoiceCB, state: FSMContext, settings: Settings):
    """✅ Оставить: the count saved before; ♾ Без лимита: 0."""
    await callback.answer()
    await state.update_data(messages_count=settings.messages_count if callback_data.value == "keep" else 0)
    await _ask_message(callback.message.edit_text, state)


@router.message(ChatPost.count)
async def count_input(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if not raw.isdecimal():
        await message.answer("Нужно целое число: сколько сообщений от каждого воркера — или «♾ Без лимита» выше.")
        return
    await state.update_data(messages_count=int(raw))
    await _ask_message(message.answer, state)


async def _save_options(message: Message, data: dict, settings: Settings, manager: JobManager, content) -> bool:
    """Write the run's trigger / messages count to config.toml right before it starts; False
    (content dropped, operator told; the step stays, to send the message again) if it can't start. No
    await follows the busy check, so the caller's manager.run takes the slot this saw free."""
    if manager.active:
        content.cleanup()
        await busy_step(message, manager, ACTIVITY)
        return False
    try:
        if "trigger" in data and data["trigger"] != settings.trigger:
            settings.set_trigger(data["trigger"])
        if data["messages_count"] != settings.messages_count:
            settings.set_messages_count(data["messages_count"])
    except (OSError, ValueError) as err:  # ValueError: a config.toml broken by hand meanwhile
        content.cleanup()
        await back_answer(message, f"⚠️ Не удалось сохранить config.toml: {err}", ACTIVITY, manager.project_id)
        return False
    return True


# ================= The message: the run =================

def _job(flow: str, data: dict, content):
    """The flow's job, for manager.run: (factory, header, done; None: the default summary)."""
    if flow == "comments":
        return lambda f, r: f.run(data["link"], content, None, r), "Рассылка в комментарии…", None
    mentions = (data.get("mention_all", False), data.get("mention_mode"), data.get("sticker_set"))
    if flow == "instant":
        return (lambda f, r: f.run(data["choice"], *mentions, content, data["link"], r),
                "Мгновенная рассылка…", None)

    async def listen(func, reporter):  # chat: the workers wait for the trigger
        broadcast = func.prepare(data["choice"], *mentions, content)
        await asyncio.gather(*func.listener_coros(broadcast, reporter))
    return listen, "Слушатель рассылки запущен", "⏹ Слушатель остановлен"


@router.message(ChatPost.message)
async def message_input(
    message: Message, state: FSMContext, album,
    pool: WorkerPool, functions: dict, manager: JobManager, settings: Settings,
):
    data = await state.get_data()

    # a stale step: no trigger either, as an empty one would fire on every media message without a caption
    flow = data.get("flow")
    if flow not in _NEEDS or not all(data.get(key) is not None and data.get(key) != "" for key in _NEEDS[flow]) \
            or "messages_count" not in data:
        await state.clear()
        await back_answer(message, STALE_STEP, ACTIVITY, manager.project_id)
        return

    content = await build_content(message, album)
    if content is None:  # the step stays: another message is asked for
        return
    if not await _save_options(message, data, settings, manager, content):
        return
    await state.clear()

    instance, bot_function = resolve(functions, flow)
    factory, header, done = _job(flow, data, content)
    started = await manager.run(message.bot, message.chat.id, pool, instance, bot_function, factory, header, done,
                                cleanup=content.cleanup)
    if started and flow == "chat":
        await message.answer(f"Отправьте триггер «{settings.trigger}» в чат. Остановить — кнопкой ⏹ Стоп выше.")
