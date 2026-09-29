import asyncio

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
from rich.console import Console

from functions.base import TelethonFunction
from functions.base.base import AccountLimited

console = Console()


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
                return res.chats[0]
            except UserAlreadyParticipantError:
                return await session.get_entity(ref)

        hash_ = self.invite_hash(link)

        try:
            res = await session(ImportChatInviteRequest(hash_))
            return res.chats[0]
        except UserAlreadyParticipantError:
            info = await session(CheckChatInviteRequest(hash_))
            return info.chat

    async def invite(self, session, source_link, destination, target_ids):
        if not target_ids:
            return

        async with self.storage.ainitialize_session(session):
            try:
                me = await session.get_me()
                source = await self.resolve_source(session, source_link)
                dest = await session.get_entity(destination)
            except Exception as err:
                console.print(f"[bold red][!] can't prepare account:[/] {err}")
                return

            added = 0
            wanted = set(target_ids)

            try:
                async for user in session.iter_participants(source):
                    if user.id not in wanted or user.bot or user.deleted or user.is_self:
                        continue

                    try:
                        await self.safe_call(lambda: session(InviteToChannelRequest(
                            channel=dest,
                            users=[user]
                        )))
                    except AccountLimited as err:
                        console.print(f"[{self.safe(me.first_name)}] [bold red]limit, stopping.[/] {self.safe(err)}")
                        break
                    except ChatAdminRequiredError:
                        console.print(f"[{self.safe(me.first_name)}] [bold red]no invite rights in destination[/]")
                        break
                    except (UserPrivacyRestrictedError, UserNotMutualContactError,
                            UserChannelsTooMuchError, UserBotError):
                        continue
                    except Exception as err:
                        console.print(f"[{self.safe(me.first_name)}] [bold red]skip[/] {user.id}: {self.safe(err)}")
                        continue
                    else:
                        added += 1
                        console.print(
                            f"[{self.safe(me.first_name)}] [bold green]invited[/] {user.id} total: [yellow]{added}[/]"
                        )

                    await self.delay()
            except Exception as err:
                console.print(f"[{self.safe(me.first_name)}] [bold red]can't read participants:[/] {self.safe(err)}")

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

        self.settings.delay = self.parse_delay(delay)

        target_ids = None

        with console.status("Parsing users...", spinner="dots"):
            for session in self.sessions:
                async with self.storage.ainitialize_session(session):
                    try:
                        source = await self.resolve_source(session, source_link)
                        participants = await session.get_participants(source)
                    except Exception as err:
                        console.print(f"[bold red][!][/] {err}")
                        continue

                    target_ids = [
                        user.id for user in participants
                        if not (user.bot or user.deleted or user.is_self)
                    ]
                    break

        if not target_ids:
            console.print("[bold red]Couldn't parse the source chat with any account")
            return

        console.print(f"[bold green][*] Parsed {len(target_ids)} users[/]")

        chunks = self.chunkify(target_ids, len(self.sessions))

        with console.status("Inviting...", spinner="dots"):
            await asyncio.gather(*[
                self.invite(session, source_link, destination, chunk)
                for session, chunk in zip(self.sessions, chunks)
            ])
