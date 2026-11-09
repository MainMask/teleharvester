"""💬 Активность: the workers writing in a chat — 🗨 Комментарии — к посту, 🆕 — к новым постам (under each new post
of the channels watched), ⚡ Чат — сразу, 🔔 Чат — по слову. One chain of steps (FSM data "flow" says which): what to
send and the mentions (the chats'), a sticker set, the chat's link or the trigger, the messages count — or 🆕's
channels, comments a post and pause —, then the message itself."""
import asyncio

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.callbacks import ChoiceCB, FunctionCB
from bot.keyboards.common import cancel_kb, choice_kb, yes_no_kb
from bot.routers._common import (CHAT_FORMAT, SEND_MESSAGE_PROMPT, STALE_STEP, back_answer, build_content, busy_step,
                                 left_flow, read_text_document, require_post_link, require_text, resolve, stale_tap,
                                 start_form, typed_channels)
from bot.routers.projects import views
from bot.services.delegation import WorkerPool
from bot.services.jobs import JobManager
from bot.services.workspaces import Workspaces
from modules import watch_state
from modules.rich_message import RichContent
from bot.states import ChatPost
from functions.broadcast import INSTANT_MODES, MENTION_ASK, MENTION_WHO, MMODE_OPTIONS, MODE_OPTIONS, STICKERS
from modules.registry import ACTIVITY, title
from modules.settings import Settings, count_text, parse_seconds

router = Router()

# what each flow asks first: its modes (instant, chat), or the post's link (comments)
_MODES = {
    "instant": (f"{title('instant')} — что отправлять?", INSTANT_MODES),
    "chat": (f"{title('chat')} — что отправлять?\n💬 Ответом на сообщение — отправьте триггер ответом на нужное "
             "сообщение: воркеры ответят на него.", MODE_OPTIONS),
}
# what its run needs, from the steps before the message: a stale message step has some missing
_NEEDS = {"comments": ("link", "messages_count"), "instant": ("choice", "link", "messages_count"),
          "chat": ("choice", "trigger", "messages_count"), "watch": ("channels", "per_post", "gap")}
VARIANTS_FLOWS = ("comments", "watch")  # their message may be a .txt of variants, one a comment
MAX_VARIANTS_FILE = 1024 * 1024
VARIANTS_ASK = "\n\nИли пришлите .txt со списком вариантов — по одному на строку: каждый комментарий возьмёт случайный."
WATCH_ASK = (f"{title('watch')} — за какими каналами следить? Пришлите их через запятую ({CHAT_FORMAT}). Если "
             f"обсуждение канала только для участников — сначала «{title('join')}».")


@router.callback_query(FunctionCB.filter(F.key.in_({"comments", "watch", "instant", "chat"})))
async def start(callback: CallbackQuery, callback_data: FunctionCB, state: FSMContext, pool: WorkerPool,
                workspaces: Workspaces):
    flow = dict(flow=callback_data.key)
    if callback_data.key == "comments":
        await start_form(callback, state, ChatPost.link, f"{title('comments')} — пришлите ссылку на пост:", pool,
                         data=flow)
        return
    if callback_data.key == "watch":
        project = await views.current(callback.message, state, workspaces)
        if project is None:
            await callback.answer()
            return
        own = [(f"📢 Каналы проекта ({len(project.channels)})", "own")] if project.channels else []
        await start_form(callback, state, ChatPost.channels, WATCH_ASK, pool,
                         choice_kb("watch_own", own) if own else None,
                         data=dict(flow, project_channels=project.channels))
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


# ================= 🆕: the channels, the comments a post, the pause =================

@router.callback_query(ChatPost.channels, ChoiceCB.filter(F.scope == "watch_own"))
async def watch_own(callback: CallbackQuery, state: FSMContext, pool: WorkerPool):
    channels = (await state.get_data()).get("project_channels")
    if not channels:  # a button of a flow gone
        await stale_tap(callback)
        return
    await callback.answer()
    await _ask_per_post(callback.message.edit_text, state, pool, channels)


@router.message(ChatPost.channels)
async def watch_channels(message: Message, state: FSMContext, pool: WorkerPool):
    raw = await require_text(message)
    channels = raw and await typed_channels(message, raw)
    if channels:
        await _ask_per_post(message.answer, state, pool, channels)


async def _ask_per_post(say, state: FSMContext, pool: WorkerPool, channels: list[str]):
    await state.update_data(channels=channels)
    await state.set_state(ChatPost.per_post)
    await say(f"Каналов: {len(channels)}. Сколько комментариев писать под каждым новым постом? Каждый — от другого "
              f"воркера (воркеров: {pool.count()}). Пришлите число или нажмите «♾ Все воркеры»:",
              reply_markup=cancel_kb(ChatPost.per_post, choice_kb("watch_all", [("♾ Все воркеры", "0")])))


@router.callback_query(ChatPost.per_post, ChoiceCB.filter(F.scope == "watch_all"))
async def per_post_all(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await _ask_gap(callback.message.edit_text, state, 0)


@router.message(ChatPost.per_post)
async def per_post_input(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if not (raw.isdecimal() and int(raw) > 0):
        await message.answer("Нужно число от 1 — или «♾ Все воркеры» выше. Пришлите ещё раз:")
        return
    await _ask_gap(message.answer, state, int(raw))


async def _ask_gap(say, state: FSMContext, per_post: int):
    """`per_post`: 0 for every worker."""
    await state.update_data(per_post=per_post)
    await state.set_state(ChatPost.gap)
    await say("Пауза перед каждым комментарием под постом, в минутах: число или диапазон, например 3-10. Первый "
              "комментарий — тоже через неё после выхода поста:", reply_markup=cancel_kb(ChatPost.gap))


@router.message(ChatPost.gap)
async def gap_input(message: Message, state: FSMContext):
    minutes = parse_seconds(message.text)  # a number or a range, as the delay's input in ⚙️
    if minutes is None:
        await message.answer("Нужно число минут или диапазон, например 3-10. Пришлите ещё раз:")
        return
    await state.update_data(gap=[minute * 60 for minute in minutes])
    await _ask_message(message.answer, state)


# ================= The trigger and the messages count =================
# Chosen per run, with the current config value one tap away; written to config.toml only
# when the job really starts (_save_options), so a run refused for a busy slot changes nothing; a
# running job keeps the ones it started with (functions/broadcast.py), whatever another project's run writes.

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
                               choice_kb("bc_trigger", [(f"✅ Оставить: «{settings.trigger[:40]}»", "keep")])),
    )


async def _ask_count(say, state: FSMContext, settings: Settings):
    await state.set_state(ChatPost.count)
    await say(
        f"Сколько сообщений отправит каждый воркер? Сейчас: {count_text(settings.messages_count)}.\n"
        f"Пришлите число{', оставьте текущее' if settings.messages_count else ''} или нажмите «♾ Без лимита» — до ⏹:",
        reply_markup=cancel_kb(ChatPost.count, choice_kb(
            "bc_count", [*([(f"✅ Оставить: {count_text(settings.messages_count)}", "keep")]
                           if settings.messages_count else []), ("♾ Без лимита", "0")])),
    )


async def _ask_message(say, state: FSMContext):
    await state.set_state(ChatPost.message)
    variants = (await state.get_data()).get("flow") in VARIANTS_FLOWS
    await say(SEND_MESSAGE_PROMPT + (VARIANTS_ASK if variants else ""), reply_markup=cancel_kb(ChatPost.message))


@router.callback_query(ChatPost.trigger, ChoiceCB.filter(F.scope == "bc_trigger"))
async def trigger_keep(callback: CallbackQuery, state: FSMContext, settings: Settings):
    if not settings.trigger:  # emptied since its button was drawn: nothing to keep
        await stale_tap(callback)
        return
    await callback.answer()
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
        await message.answer("Нужно целое число: сколько сообщений от каждого воркера — или «♾ Без лимита» выше. "
                             "Пришлите ещё раз:")
        return
    await state.update_data(messages_count=int(raw))
    await _ask_message(message.answer, state)


async def _save_options(message: Message, data: dict, settings: Settings, manager: JobManager, cleanup) -> bool:
    """Write the run's trigger / messages count to config.toml right before it starts; False
    (content dropped by `cleanup`, operator told; the step stays, to send the message again) if it can't start. No
    await follows the busy check, so the caller's manager.run takes the slot this saw free."""
    if manager.active:
        cleanup()
        await busy_step(message, manager, ACTIVITY)
        return False
    try:
        if "trigger" in data and data["trigger"] != settings.trigger:
            settings.set_trigger(data["trigger"])
        if "messages_count" in data and data["messages_count"] != settings.messages_count:  # 🆕 has none
            settings.set_messages_count(data["messages_count"])
    except (OSError, ValueError) as err:  # ValueError: a config.toml broken by hand meanwhile
        cleanup()
        await back_answer(message, f"⚠️ Не удалось сохранить config.toml: {err}", ACTIVITY, manager.project_id)
        return False
    return True


# ================= The message: the run =================

def _job(flow: str, data: dict, contents: list):
    """The flow's job, for manager.run: (factory, header, done; None: the default summary). `contents`: the message,
    or (the comments) its variants."""
    if flow == "comments":
        return lambda f, r: f.run(data["link"], contents, r), "Рассылка в комментарии…", None
    content = contents[0]
    mentions = (data.get("mention_all", False), data.get("mention_mode"), data.get("sticker_set"))
    if flow == "instant":
        return (lambda f, r: f.run(data["choice"], *mentions, content, data["link"], r),
                "Мгновенная рассылка…", None)

    async def listen(func, reporter):  # chat: the workers wait for the trigger
        broadcast = func.prepare(data["choice"], *mentions, content)
        await asyncio.gather(*func.listener_coros(broadcast, reporter))
    return listen, "Слушатель рассылки…", None


@router.message(ChatPost.message)
async def message_input(
    message: Message, state: FSMContext, album,
    pool: WorkerPool, functions: dict, manager: JobManager, settings: Settings,
):
    data = await state.get_data()

    # a stale step: no trigger either, as an empty one would fire on every media message without a caption
    flow = data.get("flow")
    if flow not in _NEEDS or not all(data.get(key) is not None and data.get(key) != "" for key in _NEEDS[flow]):
        await state.clear()
        await back_answer(message, STALE_STEP, ACTIVITY, manager.project_id)
        return

    if flow in VARIANTS_FLOWS and (message.document is not None
                                   and (message.document.file_name or "").lower().endswith(".txt")):
        contents = await _read_variants(message)
    else:
        content = await build_content(message, album)
        contents = content and [content]
    if not contents:  # the step stays: another message is asked for
        return

    def cleanup():
        for content in contents:
            content.cleanup()

    if not await _save_options(message, data, settings, manager, cleanup):
        return
    await state.clear()

    if flow == "watch":
        await _start_watch(message.bot, message.chat.id, pool, functions, manager,
                           {key: data[key] for key in _NEEDS["watch"]} | {"contents": contents})
        return
    instance, bot_function = resolve(functions, flow)
    factory, header, done = _job(flow, data, contents)
    started = await manager.run(message.bot, message.chat.id, pool, instance, bot_function, factory, header, done,
                                cleanup=cleanup)
    if started and flow == "chat":
        await message.answer(f"Отправьте триггер «{settings.trigger}» в чат. Остановить — кнопкой ⏹ Стоп выше.")


async def _read_variants(message: Message) -> list | None:
    """The .txt's variants, a comment each: its non-empty lines; None after asking for another file."""
    text = await read_text_document(message, MAX_VARIANTS_FILE)
    if text is None:
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        await message.answer("В файле нет ни одного варианта. Пришлите ещё раз:")
        return None
    return [RichContent(text=line) for line in lines]


# ================= 🆕: its run, kept on disk while it goes on (modules/watch_state.py) =================

async def _start_watch(bot, chat_id: int, pool: WorkerPool, functions: dict, manager: JobManager, watch: dict) -> bool:
    """🆕's run on the project's workers: `watch` — channels, per_post, gap, contents and, continued after a restart,
    `last` (watch_state.load). Kept on disk until it is over — ⏹, its end, a start refused —, not when the bot stops:
    the next start continues it (resume_watches)."""
    home = pool.storage.home
    if "last" not in watch:  # a new one: its message's media moved into the project's folder
        watch_state.save(home, chat_id, watch["channels"], watch["per_post"], watch["gap"], watch["contents"])

    def cleanup():
        if not watch_state.shutting_down:
            watch_state.drop(home)

    per_post = f"по {watch['per_post']}" if watch["per_post"] else "от всех воркеров"
    gap = "–".join(str(seconds // 60) for seconds in watch["gap"])
    instance, bot_function = resolve(functions, "watch")
    return await manager.run(
        bot, chat_id, pool, instance, bot_function,
        lambda f, r: f.run(watch["channels"], watch["per_post"], watch["gap"], watch["contents"], r,
                           last=watch.get("last"), on_seen=lambda last: watch_state.update_last(home, last)),
        f"Слежу за каналами ({len(watch['channels'])}): комментарии под новыми постами {per_post}, пауза {gap} мин. "
        "Остановить — ⏹.", cleanup=cleanup)


async def resume_watches(bot, workspaces):
    """Bot startup: continue each project's 🆕 a stop or a crash interrupted."""
    for workspace in workspaces.all():
        watch = watch_state.load(workspace.pool.storage.home)
        if watch is None:
            continue
        chat_id = watch["chat_id"]
        try:
            if workspace.pool.count() == 0:
                watch_state.drop(workspace.pool.storage.home)
                await bot.send_message(chat_id, f"⚠️ «{title('watch')}» не продолжена после перезапуска: у проекта "
                                                "нет воркеров.")
                continue
            await bot.send_message(chat_id, f"🔄 Бот перезапущен — продолжаю «{title('watch')}».")
        except Exception:  # the chat unreachable: the watch still goes on, its reports with it
            pass
        await _start_watch(bot, chat_id, workspace.pool, workspace.functions, workspace.manager, watch)


async def stop_watches():
    """Bot shutdown: the watches cancelled now are kept, for resume_watches."""
    watch_state.shutting_down = True
