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
from modules.account_limits import DailyCounter
from scraper.scrape import channel_slug


class InvitingFunc(TelethonFunction):
    """Invite users from supergroup"""

    # channel_slug: the scraper's parser, which drops a trailing "/", a ?query and a post id
    @staticmethod
    def is_public(link):
        return not channel_slug(link).startswith("+")  # an invite's slug is "+<hash>"

    @staticmethod
    def invite_hash(link):
        return channel_slug(link).lstrip("+")

    @staticmethod
    def public_ref(link):
        return "@" + channel_slug(link)

    @staticmethod
    def chunkify(lst, n):  # split list
        return [lst[i::n] for i in range(n)]

    @staticmethod
    def joined_chat(result):
        """The chat a join result carries (ChatInviteJoinResultOk wraps the Updates); None
        for UpdatesTooLong, which has no chats: the caller looks the chat up instead. A join
        that waits for a confirmation in the chat's bot web app is not a join: ValueError."""
        if isinstance(result, types.messages.ChatInviteJoinResultWebView):
            raise ValueError("чат требует подтверждения вступления в web-app бота")
        chats = getattr(result.updates, "chats", None)
        return chats[0] if chats else None

    async def resolve_source(self, session, link):
        """Join the source with this account and return its entity (access hashes valid for it)."""
        if self.is_public(link):
            ref = self.public_ref(link)

            try:
                if chat := self.joined_chat(await session(JoinChannelRequest(ref))):
                    return chat
            except UserAlreadyParticipantError:
                pass
            return await session.get_entity(ref)

        hash_ = self.invite_hash(link)

        try:
            if chat := self.joined_chat(await session(ImportChatInviteRequest(hash_))):
                return chat
        except UserAlreadyParticipantError:
            pass
        return (await session(CheckChatInviteRequest(hash_))).chat  # ChatInviteAlready

    async def invite(self, session, source_link, destination, target_ids, report):
        if not target_ids:
            return

        async with self.storage.ainitialize_session(session):
            try:
                me = await self.get_me(session)
                source = await self.resolve_source(session, source_link)
                dest = await session.get_entity(destination)
            except Exception as err:
                self.progress_failed()
                await report(f"[!] аккаунт не подготовлен: {err}")
                self.progress_drop(len(target_ids))
                return

            if isinstance(dest, types.Chat):  # a basic group: InviteToChannel needs a channel/supergroup
                self.progress_failed()
                await report(f"[!] [{me.first_name}] чат назначения — не супергруппа и не канал")
                self.progress_drop(len(target_ids))
                return

            added = 0
            tried = 0
            wanted = set(target_ids)
            aid = getattr(me, "id", None)

            try:
                async for user in session.iter_participants(source):
                    if user.id not in wanted or user.bot or user.deleted or user.is_self:
                        continue

                    if self._limits.reached(aid):
                        await report(f"[{me.first_name}] дневной лимит инвайтов достигнут, стоп")
                        break

                    try:
                        result = await self.safe_call(lambda: session(InviteToChannelRequest(
                            channel=dest,
                            users=[user]
                        )))
                    except AccountLimited as err:
                        self.progress_failed()
                        await report(f"[{me.first_name}] лимит, остановка. {err}")
                        break
                    except ChatAdminRequiredError:
                        self.progress_failed()
                        await report(f"[!] [{me.first_name}] нет прав приглашать в чат назначения")
                        break
                    except (UserPrivacyRestrictedError, UserNotMutualContactError,
                            UserChannelsTooMuchError, UserBotError):
                        self._limits.bump(aid)  # the request was sent: counts toward the daily cap
                    except Exception as err:
                        self.progress_failed()
                        await report(f"[{me.first_name}] пропуск {user.id}: {err}")
                    else:
                        self._limits.bump(aid)  # the request reached Telegram
                        # privacy-restricted users come back in missing_invitees, not as an
                        # error: not invited, but the request was sent, so the delay still applies
                        if not getattr(result, "missing_invitees", None):
                            added += 1
                            self.progress_ok()
                            await report(f"[{me.first_name}] приглашён {user.id}, всего: {added}")

                    tried += 1
                    self.progress_step()
                    await self.delay()
            except Exception as err:
                self.progress_failed()
                await report(f"[{me.first_name}] не удалось прочитать участников: {err}")

            # a limit / no rights / users not found in the source: the rest won't be tried
            self.progress_drop(len(target_ids) - tried)

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
        self._limits = DailyCounter(
            "stats/invite_limits.json", self.settings.invite_per_account_daily
        )
        self.progress_prepare()

        target_ids = await self.parse_targets(source_link, report)

        if not target_ids:
            self.progress_failed()
            await report("Не удалось получить участников исходного чата ни одним аккаунтом")
            return

        await report(f"[*] Получено пользователей: {len(target_ids)}")
        self.progress_total(len(target_ids))

        chunks = self.chunkify(target_ids, len(self.sessions))

        await asyncio.gather(*[
            self.invite(session, source_link, destination, chunk, report)
            for session, chunk in zip(self.sessions, chunks)
        ])

    async def execute(self):
        self.ask_accounts_count()

        if not self.sessions:
            console.print("[bold red]Нет аккаунтов")
            return

        source_link = console.input("[bold red]ссылка на чат-источник> [/]")
        destination = console.input("[bold red]куда приглашать> [/]")

        delay = Prompt.ask(
            "[bold red]задержка[/]",
            default="-".join(str(x) for x in self.settings.delay)
        )

        await self.run(source_link, destination, self.parse_delay(delay), console_report)
