import asyncio

from telethon import types
from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest
from telethon.tl.functions.channels import JoinChannelRequest, InviteToChannelRequest
from telethon.errors import (
    UserAlreadyParticipantError,
    UserPrivacyRestrictedError,
    UserNotMutualContactError,
    UserChannelsTooMuchError,
    UserBotError,
    ChatAdminRequiredError,
)

from rich.prompt import Prompt
from modules.console import console

from functions.base import TelethonFunction
from functions.base.base import AccountLimited, console_report


class InvitingFunc(TelethonFunction):
    """Invite users from supergroup"""

    @staticmethod
    def is_public(link):
        return not ("joinchat" in link or "/+" in link or link.startswith("+"))

    @staticmethod
    def invite_hash(link):
        return link.split("/")[-1].replace("+", "")

    @staticmethod
    def public_ref(link):
        if "t.me" in link:
            return "@" + link.split("/")[-1]

        return link if link.startswith("@") else "@" + link

    @staticmethod
    def chunkify(lst, n):  # split list
        return [lst[i::n] for i in range(n)]

    async def resolve_source(self, session, link):
        """Join the source with this account and return its entity (access hashes valid for it)."""
        if self.is_public(link):
            ref = self.public_ref(link)

            try:
                res = await session(JoinChannelRequest(ref))
                return res.updates.chats[0]  # ChatInviteJoinResultOk wraps the Updates
            except UserAlreadyParticipantError:
                return await session.get_entity(ref)

        hash_ = self.invite_hash(link)

        try:
            res = await session(ImportChatInviteRequest(hash_))
            return res.updates.chats[0]  # ChatInviteJoinResultOk wraps the Updates
        except UserAlreadyParticipantError:
            info = await session(CheckChatInviteRequest(hash_))
            return info.chat

    async def invite(self, session, source_link, destination, target_ids, report):
        if not target_ids:
            return

        async with self.storage.ainitialize_session(session):
            try:
                me = await session.get_me()
                source = await self.resolve_source(session, source_link)
                dest = await session.get_entity(destination)
            except Exception as err:
                await report(f"[!] can't prepare account: {err}")
                return

            if isinstance(dest, types.Chat):  # a basic group: InviteToChannel needs a channel/supergroup
                await report(f"[{me.first_name}] destination is not a supergroup/channel")
                return

            added = 0
            wanted = set(target_ids)

            try:
                async for user in session.iter_participants(source):
                    if user.id not in wanted or user.bot or user.deleted or user.is_self:
                        continue

                    try:
                        result = await self.safe_call(lambda: session(InviteToChannelRequest(
                            channel=dest,
                            users=[user]
                        )))
                    except AccountLimited as err:
                        await report(f"[{me.first_name}] limit, stopping. {err}")
                        break
                    except ChatAdminRequiredError:
                        await report(f"[{me.first_name}] no invite rights in destination")
                        break
                    except (UserPrivacyRestrictedError, UserNotMutualContactError,
                            UserChannelsTooMuchError, UserBotError):
                        continue
                    except Exception as err:
                        await report(f"[{me.first_name}] skip {user.id}: {err}")
                        continue
                    else:
                        # privacy-restricted users come back in missing_invitees, not as an
                        # error: not invited, but the request was sent, so the delay still applies
                        if not getattr(result, "missing_invitees", None):
                            added += 1
                            await report(f"[{me.first_name}] invited {user.id} total: {added}")

                    await self.delay()
            except Exception as err:
                await report(f"[{me.first_name}] can't read participants: {err}")

    async def parse_targets(self, source_link, report):
        """Resolve the source chat with the first able worker; return member ids."""
        for session in self.sessions:
            async with self.storage.ainitialize_session(session):
                try:
                    source = await self.resolve_source(session, source_link)
                    participants = await session.get_participants(source)
                except Exception as err:
                    await report(f"[!] {err}")
                    continue

                return [
                    user.id for user in participants
                    if not (user.bot or user.deleted or user.is_self)
                ]

        return None

    async def run(self, source_link, destination, delay, report):
        self.delay_range = delay

        target_ids = await self.parse_targets(source_link, report)

        if not target_ids:
            await report("Couldn't parse the source chat with any account")
            return

        await report(f"[*] Parsed {len(target_ids)} users")

        chunks = self.chunkify(target_ids, len(self.sessions))

        await asyncio.gather(*[
            self.invite(session, source_link, destination, chunk, report)
            for session, chunk in zip(self.sessions, chunks)
        ])

    async def execute(self):
        self.ask_accounts_count()

        if not self.sessions:
            console.print("[bold red]No accounts")
            return

        source_link = console.input("[bold red]link to source chat> [/]")
        destination = console.input("[bold red]where to invite users> [/]")

        delay = Prompt.ask(
            "[bold red]delay[/]",
            default="-".join(str(x) for x in self.settings.delay)
        )

        await self.run(source_link, destination, self.parse_delay(delay), console_report)
