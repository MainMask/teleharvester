from telethon.tl.functions.account import UpdateProfileRequest
from modules import profile_done
from functions.base import TelethonFunction


class ChangeBioFunc(TelethonFunction):
    """Change bio"""

    # the step after the worker picker, bot/routers/profile.py
    PROMPT = ("Пришлите новый текст bio для выбранных воркеров или .txt со списком вариантов, по одному на строку: "
              "каждый воркер получит случайный.")

    async def change_bio(self, session, report, bio=None, bios=None, taken=None):
        if bios is not None:
            bio = profile_done.pick_fresh(bios, taken)  # one no other worker has, while there is one
            taken.append(bio)
        if await self.request_each(session, report, UpdateProfileRequest(about=bio), "bio изменено", "bio не изменено"):
            self.mark_done(session, bio)

    async def run(self, report, bio=None, bios=None):
        taken = profile_done.used(type(self).__name__)
        await self.run_sequential(
            lambda session, report: self.change_bio(session, report, bio=bio, bios=bios, taken=taken),
            report,
        )
