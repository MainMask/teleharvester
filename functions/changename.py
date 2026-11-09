from typing import List, Tuple, Optional
from telethon import TelegramClient
from telethon.tl.functions.account import UpdateProfileRequest
from modules import profile_done
from functions.base import TelethonFunction
from modules.types.account import full_name


class ChangeNameFunc(TelethonFunction):
    """Change names"""

    # the step after the worker picker, bot/routers/profile.py
    PROMPT = ("Пришлите имя для выбранных воркеров (например, «Иван Петров») или .txt со списком имён, по одному "
              "на строку: каждый воркер получит случайное.")
    
    @staticmethod
    def get_random_name(names: List[str], taken=()) -> Tuple[str, Optional[str]]:
        """A random name from the list, one not in `taken` (full names) while there is one."""
        name = profile_done.pick_fresh([" ".join(name.split()) for name in names], list(taken)).split()

        if len(name) == 1:
            return name[0], None

        return name[0], " ".join(name[1:])
    
    @staticmethod
    def current_names(storage) -> list[str]:
        """The names the workers have now (their .jsessions): a name from a list goes to one worker only, while it
        lasts (the warmup's too)."""
        return [full_name(js.account.account.first_name, js.account.account.last_name)
                for js in storage.jsessions_paths.values()]

    async def change_name(
        self,
        session: TelegramClient,
        report,
        names: Optional[List[str]] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        taken: Optional[List[str]] = None,
    ):
        async with self.storage.ainitialize_session(session):
            me = await self.poll_me(session, report)
            if me is None:
                return

            if names is not None:  # picked once it is polled: a worker skipped takes no name from the others
                first_name, last_name = self.get_random_name(names, taken)
                taken.append(full_name(first_name, last_name))

            old_name = full_name(me.first_name, me.last_name)

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
                new_name = full_name(first_name, last_name)
                self.progress_ok()
                await report(f"Имя изменено: {old_name} → {new_name}")

    async def run(self, report, names=None, first_name=None, last_name=None):
        taken = self.current_names(self.storage) if names is not None else []
        await self.run_sequential(
            lambda session, report: self.change_name(
                session, report,
                names=names, first_name=first_name, last_name=last_name, taken=taken
            ),
            report,
        )

