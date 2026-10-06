import random
import os

from telethon import TelegramClient, functions

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
                await report(f"get_me failed: {err}")
                return

            try:
                await session(functions.photos.UploadProfilePhotoRequest(
                    file=await session.upload_file(photo_path),
                ))
            except Exception as err:
                await report(f"[{me.first_name}] Error : {err}")
            else:
                await report(f"[{me.first_name}] Photo uploaded successfully ({photo_path})")

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
            await report(f"No photos in {path}")
            return

        await self.run_sequential(
            lambda session, report: self.set_profile_photo(session, os.path.join(path, random.choice(photos)), report),
            report,
        )

    async def execute(self):
        self.ask_accounts_count()

        folder = os.path.join(os.getcwd(), "assets", "photos")
        while True:
            photo = console.input(
                "\n[bold white]path to one photo for every account"
                f"\n(blank = a random photo per account from {folder})> [/]"
            ).strip().strip("'\"")  # a path dragged into the terminal comes quoted
            if not photo or os.path.isfile(photo):
                break
            console.print(f"[bold red]File not found: {photo}[/]")

        await self.run(console_report, photo_path=photo or None)
