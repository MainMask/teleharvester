import os

from telethon import TelegramClient, functions, utils

from modules import profile_done
from modules.console import console

from functions.base import TelethonFunction
from functions.base.base import console_report


class ChangeProfilePhotoFunc(TelethonFunction):
    """Change profile photo"""

    async def set_profile_photo(self, session: TelegramClient, photo_path: str, report):
        async with self.storage.ainitialize_session(session):
            try:
                me = await self.get_me(session)
            except Exception as err:
                self.progress_failed()
                await report(f"не удалось опросить аккаунт: {err}")
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

        path = os.path.join(os.getcwd(), "assets", "photos")
        # regular, non-hidden files only: macOS drops a .DS_Store into any opened folder
        photos = [
            f for f in os.listdir(path)
            if not f.startswith(".") and os.path.isfile(os.path.join(path, f))
        ] if os.path.isdir(path) else []

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

    async def execute(self):
        self.ask_workers()

        folder = os.path.join(os.getcwd(), "assets", "photos")
        while True:
            photo = console.input(
                "\n[bold white]путь к одному фото для всех аккаунтов"
                f"\n(пусто — случайное фото каждому аккаунту из {folder})> [/]"
            ).strip().strip("'\"")  # a path dragged into the terminal comes quoted
            if not photo or os.path.isfile(photo):
                break
            console.print(f"[bold red]Файл не найден: {photo}[/]")

        await self.run(console_report, photo_path=photo or None)
