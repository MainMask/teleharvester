import asyncio
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
                me = await session.get_me()
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

    async def run(self, report):
        path = os.path.join(os.getcwd(), "assets", "photos")
        # regular, non-hidden files only: macOS drops a .DS_Store into any opened folder
        photos = [
            f for f in os.listdir(path)
            if not f.startswith(".") and os.path.isfile(os.path.join(path, f))
        ] if os.path.isdir(path) else []

        if not photos:
            await report(f"No photos in {path}")
            return

        await asyncio.gather(*[
            self.set_profile_photo(session, os.path.join(path, random.choice(photos)), report)
            for session in self.sessions
        ])

    async def execute(self):
        self.ask_accounts_count()

        path = os.path.join(os.getcwd(), "assets", "photos")
        console.input(
            f"\n[bold white]will be used photos from folder {path}"
            "\nPress [Enter] to continue[/]"
        )

        await self.run(console_report)
