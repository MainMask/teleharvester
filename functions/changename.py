from typing import List, Tuple, Optional
from telethon import TelegramClient
from telethon.tl.functions.account import UpdateProfileRequest
from modules import profile_done
from modules.console import console
from functions.base import TelethonFunction
from functions.base.base import console_report


class ChangeNameFunc(TelethonFunction):
    """Change names"""
    
    @staticmethod
    def get_random_name(names: List[str], taken=()) -> Tuple[str, Optional[str]]:
        """A random name from the list, one not in `taken` (full names) while there is one."""
        name = profile_done.pick_fresh([" ".join(name.split()) for name in names], list(taken)).split()

        if len(name) == 1:
            return name[0], None

        return name[0], " ".join(name[1:])
    
    async def change_name(
        self,
        session: TelegramClient,
        report,
        names: Optional[List[str]] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        taken: Optional[List[str]] = None,
    ):
        if names is not None:
            first_name, last_name = self.get_random_name(names, taken)
            taken.append(" ".join(p for p in (first_name, last_name) if p))

        async with self.storage.ainitialize_session(session):
            try:
                me = await self.get_me(session)
            except Exception as err:
                self.progress_failed()
                await report(f"не удалось опросить аккаунт: {err}")
                return

            full_name = (me.first_name or "") + (" " + me.last_name if me.last_name else "")

            try:
                await session(
                    UpdateProfileRequest(
                        first_name=first_name,
                        last_name=last_name or ""
                    )
                )
            except Exception as error:
                self.progress_failed()
                await report(f"[!] {error}")
            else:
                self.storage.remember_name(session, first_name, last_name or None)
                self.mark_done(session)
                new_name = " ".join(p for p in (first_name, last_name) if p)
                self.progress_ok()
                await report(f"Имя изменено: {full_name} → {new_name}")

    async def run(self, report, names=None, first_name=None, last_name=None):
        # the names the workers have now: a name from the list goes to one worker only, while it lasts
        taken = [
            " ".join(p for p in (js.account.account.first_name, js.account.account.last_name) if p)
            for js in self.storage.jsessions_paths.values()
        ] if names is not None else []
        await self.run_sequential(
            lambda session, report: self.change_name(
                session, report,
                names=names, first_name=first_name, last_name=last_name, taken=taken
            ),
            report,
        )

    async def execute(self):
        self.ask_workers()

        from_file = console.input("[bold red]из файла? (y/n)> ")

        if from_file == "y":
            try:
                with open("assets/names.txt", encoding="utf-8") as file:
                    names = [line for line in file.read().splitlines() if line.strip()]
            except FileNotFoundError:
                console.print("[bold red]Файл assets/names.txt не найден!")
                return

            if not names:
                console.print("[bold red]Список имён пуст!")
                return

            await self.run(console_report, names=names)

        else:
            name = console.input("[bold red]имя> [/]").split(maxsplit=1)

            while not name:
                name = console.input("[bold red]имя> [/]").split(maxsplit=1)

            print()

            first_name = name[0]
            last_name = name[1] if len(name) == 2 else None

            await self.run(console_report, first_name=first_name, last_name=last_name)

