import os

from telethon import TelegramClient, functions, utils

from modules import content, profile_done, warmup

from functions.base import TelethonFunction


class ChangeProfilePhotoFunc(TelethonFunction):
    """Change profile photo"""

    async def set_profile_photo(self, session: TelegramClient, photo_path: str, report):
        async with self.storage.ainitialize_session(session):
            me = await self.poll_me(session, report)
            if me is None:
                return

            try:
                result = await session(functions.photos.UploadProfilePhotoRequest(
                    file=await session.upload_file(photo_path),
                ))
            except Exception as err:
                self.progress_failed()
                await report(f"[{me.first_name}] ошибка: {err}")
                return
            self.mark_done(session, os.path.basename(photo_path))

            # the new one first, then the old ones: the account is never left without an avatar
            try:
                old = [p for p in await session.get_profile_photos("me") if p.id != result.photo.id]
                if old:
                    await session(functions.photos.DeletePhotosRequest(id=[utils.get_input_photo(p) for p in old]))
            except Exception as err:
                self.progress_ok()
                await report(f"[{me.first_name}] фото загружено ({photo_path}), старые не удалены: {err}")
            else:
                self.progress_ok()
                await report(f"[{me.first_name}] фото загружено ({photo_path}), старых удалено: {len(old)}")

    async def run(self, report, photo_path=None):
        if photo_path is not None:  # one photo for every account (sent through the bot)
            await self.run_sequential(
                lambda session, report: self.set_profile_photo(session, photo_path, report),
                report,
            )
            return

        # the project's region's photos (modules/content.py)
        region = warmup.region(self.storage.home)
        path = content.path(region, "photos")
        photos = [os.path.basename(photo) for photo in content.files(region, "photos")]

        if not photos:
            self.progress_failed()
            await report(f"Нет фото в {path}")
            return

        taken = profile_done.used(type(self).__name__)

        def pick():  # a photo no other worker has, while there is one
            photo = profile_done.pick_fresh(photos, taken)
            taken.append(photo)
            return os.path.join(path, photo)

        await self.run_sequential(
            lambda session, report: self.set_profile_photo(session, pick(), report),
            report,
        )
