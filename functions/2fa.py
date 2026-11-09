
from modules import tdata_import

from telethon import TelegramClient
from functions.base import TelethonFunction


class SetPasswordFunc(TelethonFunction):
    """Set two-step verification password to accounts"""

    async def edit_2fa(self, session: TelegramClient, password: str, report):
        async with self.storage.ainitialize_session(session):
            me = await self.poll_me(session, report)
            if me is None:
                return

            json_session = self.storage.jsessions_paths.get(self.storage.get_session_path(session))
            current_password = json_session.account.password if json_session else None

            try:
                await session.edit_2fa(
                    current_password=current_password,
                    new_password=password,
                )
            except Exception as err:
                self.progress_failed()
                await report(f"[{me.first_name}] пароль не изменён: {err}")
            else:
                self.mark_done(session)
                self.progress_ok()
                await report(f"[{me.first_name}] пароль изменён")
                try:  # so a later change knows the current one, and a re-import of its tdata too
                    tdata_import.remember_password(self.storage, session, password)
                except Exception as err:
                    await report(f"[{me.first_name}] пароль не сохранён: {err}")

    async def run(self, password: str, report):
        await self.run_sequential(
            lambda session, report: self.edit_2fa(session, password, report),
            report,
        )

