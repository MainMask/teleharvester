import itertools

from telethon.errors import UsernameInvalidError, UsernamePurchaseAvailableError
from telethon.tl.functions.account import UpdateUsernameRequest, CheckUsernameRequest
from modules.console import console

from functions.base import TelethonFunction
from functions.base.base import console_report

MAX_ATTEMPTS = 20  # candidates checked per account before giving up


class ChangeUsernameFunc(TelethonFunction):
    """Change usernames"""

    async def generate_username(self, session, base, counter):
        """First free of base, base1, base2, ...; the counter is shared so accounts never pick the same one."""
        for _ in range(MAX_ATTEMPTS):
            n = next(counter)
            candidate = base if n == 0 else f"{base}{n}"

            try:
                if await session(CheckUsernameRequest(candidate)):
                    return candidate
            # this candidate only: on sale at Fragment, or invalid (e.g. a base under 5 characters
            # whose base1 is fine); an account's own error (a long flood wait) still ends the search
            except (UsernamePurchaseAvailableError, UsernameInvalidError):
                continue

        return None

    async def change(self, session, report, username=None, base=None, counter=None):
        async with self.storage.ainitialize_session(session):
            try:
                me = await session.get_me()
            except Exception as err:
                await report(f"get_me failed: {err}")
                return

            if base is not None:
                try:
                    username = await self.generate_username(session, base, counter)
                except Exception as err:
                    await report(f"[{me.first_name}] не удалось подобрать username: {err}")
                    return

                if not username:
                    await report(f"[{me.first_name}] couldn't find a free username")
                    return

            try:
                await session(UpdateUsernameRequest(username))
            except Exception as err:
                await report(f"[{me.first_name}] not changed: {err}")
            else:
                self.storage.remember_username(session, username)  # orders the pool
                await report(f"[{me.first_name}] username set: @{username}")

    async def run(self, report, usernames=None, base=None):
        if usernames is not None:
            if len(usernames) < len(self.sessions):
                await report(
                    f"[!] usernames в файле: {len(usernames)}, аккаунтов: {len(self.sessions)} — "
                    f"{len(self.sessions) - len(usernames)} аккаунт(ов) без имени будут пропущены"
                )
            pairs = dict(zip(self.sessions, usernames))  # accounts beyond the file are skipped
            await self.gather_in_order(
                lambda session, report: self.change(session, report, username=pairs[session]),
                report,
                sessions=list(pairs),
            )
        else:
            base = base.strip().lstrip("@")
            counter = itertools.count()
            await self.gather_in_order(
                lambda session, report: self.change(session, report, base=base, counter=counter),
                report,
            )

    async def execute(self):
        self.ask_accounts_count()

        from_file = console.input("[bold red]from file? (y/n)> ")

        if from_file == "y":
            try:
                with open("assets/usernames.txt", encoding="utf-8") as file:
                    usernames = [line.strip() for line in file if line.strip()]
            except FileNotFoundError:
                console.print("[bold red]File assets/usernames.txt not found!")
                return

            if not usernames:
                console.print("[bold red]Usernames list is empty!")
                return

            await self.run(console_report, usernames=usernames)
        else:
            base = console.input("[bold red]base username> [/]")

            await self.run(console_report, base=base)
