from telethon.tl.functions.account import UpdateProfileRequest
from modules import profile_done
from functions.base import TelethonFunction


class ChangeBioFunc(TelethonFunction):
    """Change bio"""

    # the step after the worker picker, bot/routers/profile.py
    PROMPT = ("Пришлите новый текст bio для выбранных воркеров или .txt со списком вариантов, по одному на строку: "
              "каждый воркер получит случайный.")

    async def change_bio(self, session, report, bio=None, bios=None, taken=None):
        def request():
            nonlocal bio
            if bios is not None:  # picked once it is polled: a worker skipped takes no bio from the others
                bio = profile_done.pick_fresh(bios, taken)  # one no other worker has, while there is one
                taken.append(bio)
            return UpdateProfileRequest(about=bio)

        if await self.request_each(session, report, request, "bio изменено", "bio не изменено"):
            self.mark_done(session, bio)

    async def run(self, report, bio=None, bios=None):
        taken = profile_done.used(type(self).__name__)
        await self.run_sequential(
            lambda session, report: self.change_bio(session, report, bio=bio, bios=bios, taken=taken),
            report,
        )
