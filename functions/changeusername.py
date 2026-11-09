import random

from telethon.errors import UsernameInvalidError, UsernamePurchaseAvailableError
from telethon.tl.functions.account import UpdateUsernameRequest, CheckUsernameRequest

from functions.base import TelethonFunction

MAX_ATTEMPTS = 20  # candidates checked per account before giving up


async def first_free(session, candidates, used):
    """The first of `candidates` that is free (also the warmup's, functions/warmup.py); `used` is shared
    across accounts so they never pick the same one. None if none is."""
    for candidate in candidates:
        if candidate in used:
            continue
        used.add(candidate)  # before the answer: another account looking at once must not pick it too

        try:
            if await session(CheckUsernameRequest(candidate)):
                return candidate
        # this candidate only: on sale at Fragment, or invalid (e.g. a base under 5 characters
        # whose suffixed form is fine); an account's own error (a long flood wait) still ends the search
        except (UsernamePurchaseAvailableError, UsernameInvalidError):
            continue

    return None


class ChangeUsernameFunc(TelethonFunction):
    """Change usernames"""

    # the step after the worker picker, bot/routers/profile.py
    PROMPT = ("Пришлите основу для username (если занята — добавится случайное число: base24, base3071, …) или "
              ".txt со списком username, по одному на строку: воркеры получат их по порядку.")

    async def generate_username(self, session, base, used):
        """First free of base, then base + a random 2-4 digit suffix (e.g. CitadelCurator24) (and the
        suffixes aren't a tell-tale 1,2,3,…)."""
        return await first_free(session, (base, *(f"{base}{random.randrange(10, 10000)}"
                                                  for _ in range(MAX_ATTEMPTS))), used)

    async def change(self, session, report, username=None, base=None, used=None):
        async with self.storage.ainitialize_session(session):
            me = await self.poll_me(session, report)
            if me is None:
                return

            if base is not None:
                try:
                    username = await self.generate_username(session, base, used)
                except Exception as err:
                    self.progress_failed()
                    await report(f"[{me.first_name}] не удалось подобрать username: {err}")
                    return

                if not username:
                    self.progress_failed()
                    await report(f"[{me.first_name}] не удалось найти свободный username")
                    return

            try:
                await session(UpdateUsernameRequest(username))
            except Exception as err:
                self.progress_failed()
                await report(f"[{me.first_name}] username не изменён: {err}")
            else:
                self.storage.remember_username(session, username)  # orders the pool
                self.mark_done(session)
                self.progress_ok()
                await report(f"[{me.first_name}] username установлен: @{username}")

    async def run(self, report, usernames=None, base=None):
        if usernames is not None:
            # a list sent again for new workers: the lines the old ones hold already would fail
            taken = {username.lower() for username in self.storage.usernames.values()}
            free = [username for username in usernames if username.lower() not in taken]
            if len(free) < len(usernames):
                await report(f"[i] пропущено username, уже занятых вашими воркерами: {len(usernames) - len(free)}")
            usernames = free

            if len(usernames) < len(self.sessions):
                await report(
                    f"[!] usernames в файле: {len(usernames)}, аккаунтов: {len(self.sessions)} — "
                    f"{len(self.sessions) - len(usernames)} аккаунт(ов) без имени будут пропущены"
                )
            pairs = dict(zip(self.sessions, usernames))  # accounts beyond the file are skipped
            await self.run_sequential(
                lambda session, report: self.change(session, report, username=pairs[session]),
                report,
                sessions=list(pairs),
            )
        else:
            base = base.strip().lstrip("@")
            used = set()
            await self.run_sequential(
                lambda session, report: self.change(session, report, base=base, used=used),
                report,
            )
