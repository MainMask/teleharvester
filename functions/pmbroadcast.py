import re


from functions.base import TelethonFunction
from functions.base.base import AccountLimited
from modules import login as sign_in
from modules import rich_message


def is_phone(peer: str) -> bool:
    """A phone number (+7 999 123-45-67), not a @username or a t.me link: a username never starts with a digit."""
    return re.fullmatch(r"\+?[\d\s()-]+", peer) is not None and sign_in.normalize_phone(peer) is not None


class PmBroadcastFunc(TelethonFunction):
    """Broadcast to PM"""

    async def broadcast(self, session, peer, content, by_phone_number, report):
        async with self.storage.ainitialize_session(session):
            me = await self.poll_me(session, report)
            if me is None:
                return

            if by_phone_number:
                try:
                    users = await self.import_phone_contact(session, peer)
                except Exception as err:
                    # one account's failure must not abort the gather for the others
                    self.progress_failed()
                    await report(f"[{me.first_name}] не удалось найти номер {peer}: {err}")
                    return

                if not users:
                    self.progress_failed()
                    await report(f"[{me.first_name}] не удалось найти номер {peer}")
                    return

                peer = users[0]

            try:
                await rich_message.send(session, peer, content, self.safe_call, report=report)
            except AccountLimited as err:
                self.progress_failed()
                await report(f"[{me.first_name}] лимит: {err}")
            except Exception as err:
                self.progress_failed()
                reason = rich_message.refusal_reason(err)
                await report(f"[{me.first_name}] не отправлено: {reason or err}")
            else:
                self.progress_ok()
                await report(f"[{me.first_name}] отправлено.")

    async def run(self, peer, content, by_phone_number, report):
        await self.run_sequential(
            lambda session, report: self.broadcast(session, peer, content, by_phone_number, report),
            report,
            pause="delay",
        )
