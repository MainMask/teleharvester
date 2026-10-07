"""A permanently restricted worker's contacts go to the other workers only on the admin's button."""

import asyncio
import types

from test_bases import FOREVER, _CheckedSession, _checked_storage


def ns(**kw):
    return types.SimpleNamespace(**kw)


UNTIL = "Unfortunately...\nyou are limited until 12 Nov 2026."


def _scan(storage, sessions, replies=False):
    fn, msgs, _ = _run(storage, sessions, replies)
    return fn, msgs


def _run(storage, sessions, replies=False):
    from functions.spamblock import SpamBlockFunc

    fn = SpamBlockFunc(storage, ns(delay=[0]))
    fn.sessions = sessions
    msgs = []

    async def report(text):
        msgs.append(text)

    blocks = asyncio.run(fn.run(report, replies=replies))
    return fn, msgs, blocks


class _Slow(_CheckedSession):
    """Answers last: the report must still follow the worker order."""

    def conversation(self, *_):
        conv = super().conversation()

        class _Late:
            async def __aenter__(self):
                await asyncio.sleep(0.05)
                return await conv.__aenter__()

            async def __aexit__(self, *exc):
                return await conv.__aexit__(*exc)

        return _Late()


def test_waiting_lists_restricted_workers_with_contacts_until_released(monkeypatch, tmp_path):
    from modules import contacts_ledger, restricted_workers

    a, b, c = (_CheckedSession(1, tmp_path, reply=FOREVER), _CheckedSession(2, tmp_path, reply=FOREVER),
               _CheckedSession(3, tmp_path))
    contacts_ledger.save({"10": 1, "11": 1, "12": 3})  # b has no contacts: nothing to decide
    storage = _checked_storage(monkeypatch, tmp_path, [a, b, c])

    fn, msgs = _scan(storage, [a, b, c])
    assert fn.waiting() == [(a.path, 1, "@w1", 2)]
    assert "⛔ @w1 — ограничен бессрочно, его контакты (2) ждут решения" in msgs

    restricted_workers.release(a.path)
    fn, msgs = _scan(storage, [a, b, c])
    assert fn.waiting() == []
    assert "⛔ @w1 — ограничен бессрочно, его контакты (2) переданы другим воркерам" in msgs


def test_the_decision_is_reset_when_the_worker_is_clean_again(monkeypatch, tmp_path):
    from modules import restricted_workers

    a = _CheckedSession(1, tmp_path)
    storage = _checked_storage(monkeypatch, tmp_path, [a])
    restricted_workers.save([a.path])
    restricted_workers.save_released([a.path, str(tmp_path / "sessions" / "gone.jsession")])

    _scan(storage, [a])

    assert restricted_workers.load() == [] and restricted_workers.load_released() == []


# --- the bot: the message with the button, and the button --------------------------------------

class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((text, kwargs.get("reply_markup")))


class _Msg:
    def __init__(self):
        import datetime

        self.edits = []
        self.date = datetime.datetime.now(datetime.timezone.utc)

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs.get("reply_markup")))


class _Callback:
    def __init__(self):
        self.message = _Msg()
        self.alert = None

    async def answer(self, text=None, show_alert=False, **kwargs):
        if show_alert:
            self.alert = text


def test_the_check_offers_the_button(tmp_path):
    from bot.callbacks import ReleaseCB
    from bot.routers import spamblock

    bot = _Bot()
    func = ns(waiting=lambda: [("sessions/a.jsession", 7, "@a", 12)])
    asyncio.run(spamblock.offer_release(bot, 1, func))

    (text, markup), = bot.sent
    assert text.startswith("⛔ @a — ограничен бессрочно\nЕго контакты (12) ждут решения")
    button = markup.inline_keyboard[0][0]
    assert button.text == "Передать контакты (12)"
    assert ReleaseCB.unpack(button.callback_data).user_id == 7


def _pool(path, user_id):
    js = ns(account=ns(account=ns(user_id=user_id)))
    return ns(storage=ns(jsessions_paths={path: js}, usernames={path: "a"}))


def test_the_button_releases_the_contacts(tmp_path):
    from bot.callbacks import ReleaseCB
    from bot.routers import spamblock
    from modules import restricted_workers

    restricted_workers.save(["sessions/a.jsession"])
    callback = _Callback()
    asyncio.run(spamblock.release(callback, ReleaseCB(user_id=7), _pool("sessions/a.jsession", 7)))

    assert restricted_workers.load_released() == ["sessions/a.jsession"]
    (text, markup), = callback.message.edits
    assert text.startswith("✅ Контакты @a переданы") and markup is None


def test_the_button_does_nothing_for_a_worker_no_longer_restricted(tmp_path):
    from bot.callbacks import ReleaseCB
    from bot.routers import spamblock
    from modules import restricted_workers

    callback = _Callback()  # the last check found it clean again
    asyncio.run(spamblock.release(callback, ReleaseCB(user_id=7), _pool("sessions/a.jsession", 7)))

    assert restricted_workers.load_released() == []
    assert callback.message.edits[0][0] == "Воркер уже не ограничен бессрочно — передавать нечего."


# --- review fixes ----------------------------------------------------------------------------

def test_moving_restricted_sessions_keeps_the_ones_whose_people_wait(monkeypatch, tmp_path):
    from modules import contacts_ledger, restricted_workers

    monkeypatch.chdir(tmp_path)
    waiting, dated, released, alone = (_CheckedSession(1, tmp_path, reply=FOREVER), _CheckedSession(2, tmp_path, reply=UNTIL),
                                       _CheckedSession(3, tmp_path, reply=FOREVER), _CheckedSession(4, tmp_path, reply=FOREVER))
    contacts_ledger.save({"10": 1, "11": 2, "12": 3})  # `alone` has no contacts
    storage = _checked_storage(monkeypatch, tmp_path, [waiting, dated, released, alone])
    restricted_workers.save([released.path])
    restricted_workers.save_released([released.path])

    fn, _, blocks = _run(storage, [waiting, dated, released, alone])
    kept = fn.move_restricted(blocks)

    assert kept == ["@w1", "@w2"]
    assert (tmp_path / "sessions" / "w1.jsession").exists() and (tmp_path / "sessions" / "w2.jsession").exists()
    assert (tmp_path / "sessions" / "restricted" / "permanent" / "w3.jsession").exists()
    assert (tmp_path / "sessions" / "restricted" / "permanent" / "w4.jsession").exists()


def test_only_permanent_replies_are_quoted_in_worker_order(monkeypatch, tmp_path):
    a, b, c = _Slow(1, tmp_path, reply=FOREVER), _CheckedSession(2, tmp_path, reply=UNTIL), _CheckedSession(3, tmp_path, reply=FOREVER)
    storage = _checked_storage(monkeypatch, tmp_path, [a, b, c])

    _, msgs = _scan(storage, [a, b, c], replies=True)

    assert [m for m in msgs if m.startswith("💬")] == [f"💬 Ответ @SpamBot (@w1, @w3):\n{FOREVER}"]


def test_the_ledger_is_read_once_per_check(monkeypatch, tmp_path):
    from modules import contacts_ledger

    workers = [_CheckedSession(n, tmp_path, reply=FOREVER) for n in (1, 2, 3)]
    storage = _checked_storage(monkeypatch, tmp_path, workers)
    reads = []
    load = contacts_ledger.load
    monkeypatch.setattr(contacts_ledger, "load", lambda: reads.append(1) or load())

    _scan(storage, workers)

    assert len(reads) == 1


def test_a_stopped_check_still_reports_the_checked_workers(monkeypatch, tmp_path):
    class _Stopped(_CheckedSession):
        def conversation(self, *_):
            class _Cancel:
                async def __aenter__(self):
                    await asyncio.sleep(0.05)  # the others are done by now
                    raise asyncio.CancelledError

                async def __aexit__(self, *exc):
                    return False

            return _Cancel()

    a, b = _CheckedSession(1, tmp_path, reply=FOREVER), _Stopped(2, tmp_path)
    storage = _checked_storage(monkeypatch, tmp_path, [a, b])
    msgs = []

    async def report(text):
        msgs.append(text)

    from functions.spamblock import SpamBlockFunc

    fn = SpamBlockFunc(storage, ns(delay=[0]))
    fn.sessions = [a, b]
    try:
        asyncio.run(fn.run(report))
    except asyncio.CancelledError:
        pass

    assert msgs == ["⛔ @w1 — ограничен бессрочно"]


def test_a_stale_button_releases_nothing(tmp_path):
    import datetime

    from bot.callbacks import ReleaseCB
    from bot.routers import spamblock
    from modules import restricted_workers

    restricted_workers.save(["sessions/a.jsession"])
    callback = _Callback()
    callback.message.date = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=25)
    asyncio.run(spamblock.release(callback, ReleaseCB(user_id=7), _pool("sessions/a.jsession", 7)))

    assert restricted_workers.load_released() == []
    assert callback.message.edits == [] and "устарела" in callback.alert


def test_a_button_on_an_inaccessible_message_releases_nothing(tmp_path):
    from aiogram.types import InaccessibleMessage

    from bot.callbacks import ReleaseCB
    from bot.routers import spamblock
    from modules import restricted_workers

    restricted_workers.save(["sessions/a.jsession"])
    callback = _Callback()
    callback.message = InaccessibleMessage(chat={"id": 1, "type": "private"}, message_id=1, date=0)
    asyncio.run(spamblock.release(callback, ReleaseCB(user_id=7), _pool("sessions/a.jsession", 7)))

    assert restricted_workers.load_released() == [] and "устарела" in callback.alert
