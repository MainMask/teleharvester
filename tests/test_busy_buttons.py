"""A screen's button that starts a job or a form while the project's job slot is busy: said in a pop-up over the
screen, as every other button's "can't run now" — no message piled up under it, nothing started."""
import asyncio

import pytest

from bot.routers import spamblock
from bot.routers import warmup as warmup_router
from bot.routers.accounts import proxies, upload
from bot.services.jobs import JobManager, busy

from test_warmup_bot import _Callback, _Pool, _Storage

BUSY = f"{busy('Рассылка')}\nКогда она закончится, нажмите ещё раз."


class _State:
    def __init__(self):
        self.state = None

    async def clear(self):
        self.state = None

    async def set_state(self, state):
        self.state = state


def _busy_slot() -> JobManager:
    manager = JobManager()
    assert manager.acquire("Рассылка", cancelable=False, timeout=0)
    return manager


@pytest.mark.parametrize("tap", [
    lambda cb, pool, manager: warmup_router.warmup_run(cb, pool, {}, manager),
    lambda cb, pool, manager: spamblock.run_status(cb, pool, {}, manager, None),
    lambda cb, pool, manager: proxies.proxy_start(cb, _State(), pool, manager),
    lambda cb, pool, manager: upload.files_start(cb, _State(), manager),
], ids=["warmup", "spamblock", "proxy", "files"])
def test_a_button_tapped_while_the_slot_is_busy_says_so_in_a_pop_up(tap):
    callback = _Callback()
    asyncio.run(tap(callback, _Pool(_Storage(["a.session"])), _busy_slot()))
    assert callback.alerts == [BUSY]
    assert callback.message.answers == [] and callback.message.edits == []
