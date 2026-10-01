import asyncio
import random
import os

from telethon import TelegramClient, functions

from rich.progress import track
from rich.console import Console

from functions.base import TelethonFunction
console = Console()


class ChangeProfilePhotoFunc(TelethonFunction):
    """Change profile photo"""
    
    async def set_profile_photo(self, session: TelegramClient, photo_path: str):
        async with self.storage.ainitialize_session(session):
            try:
                me = await session.get_me()
            except Exception as err:
                console.print(f"[bold red]get_me failed:[/] {self.safe(err)}")
                return

            try:
                await session(functions.photos.UploadProfilePhotoRequest(
                    file=await session.upload_file(photo_path),
                ))
            except Exception as err:
                console.print(
                    "[{name}] [bold red]Error[/] : {err}"
                    .format(name=self.safe(me.first_name), err=self.safe(err))
                )
            else:
                console.print(
                    "[{name}] Photo uploaded [bold green]successfully[/] ({photo_path})"
                    .format(name=self.safe(me.first_name), photo_path=photo_path)
                )


    async def execute(self):
        self.ask_accounts_count()

        path = os.path.join(os.getcwd(), "assets", "photos")
        console.input(
            f"\n[bold white]will be used photos from folder {path}"
            "\nPress [Enter] to continue[/]"
        )
        
        photos = os.listdir(path) if os.path.isdir(path) else []

        if not photos:
            console.print(f"[bold red]No photos in {path}")
            return

        await asyncio.gather(*[
            self.set_profile_photo(session, os.path.join(path, random.choice(photos)))
            for session in self.sessions
        ])            
