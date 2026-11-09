import asyncio


from telethon import events, types
from telethon.errors import UserAlreadyParticipantError
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.channels import GetFullChannelRequest

from functions.base import TelethonFunction
from functions.base.base import AccountLimited
from scraper.scrape import channel_slug

CAPTCHA_WAIT = 30  # seconds a worker waits for a captcha after joining


class JoinerFunc(TelethonFunction):
    """Join chat"""

    async def join(self, session, link, index, mode, report) -> bool:
        """True if the worker is in the chat now (`mode` "2": the channel's comments group); else reports why."""
        # channel_slug: the scraper's parser, which drops a trailing "/", a ?query and a post id
        slug = channel_slug(link)

        if mode == "1":
            try:
                if not slug.startswith("+"):  # an invite's slug is "+<hash>"
                    updates = await self.call(session, JoinChannelRequest("@" + slug))
                else:
                    updates = await self.call(session, ImportChatInviteRequest(slug[1:]))
            except UserAlreadyParticipantError:  # only an invite raises it: the worker is in the chat already
                return True
            except AccountLimited as error:
                await report(f"[!] [аккаунт {index + 1}] лимит: {error}")
                return False
            except Exception as error:
                await report(f"[!] [аккаунт {index + 1}] {error}")
                return False
            if isinstance(updates, types.messages.ChatInviteJoinResultWebView):
                await report(f"[!] [аккаунт {index + 1}] чат требует подтверждения вступления в web-app бота")
                return False
            return True

        elif mode == "2":
            try:
                full = await self.call(session, GetFullChannelRequest("@" + slug))
                linked_id = full.full_chat.linked_chat_id

                if not linked_id:
                    await report(f"[!] [аккаунт {index + 1}] у канала нет привязанного чата")
                    return False

                chat = next((c for c in full.chats if c.id == linked_id), None)

                if chat is None:
                    await report(f"[!] [аккаунт {index + 1}] привязанный чат не найден")
                    return False

                await self.call(session, JoinChannelRequest(chat))
            except AccountLimited as error:
                await report(f"[!] [аккаунт {index + 1}] лимит: {error}")
                return False
            except Exception as error:
                await report(f"[!] [аккаунт {index + 1}] {error}")
                return False
            return True

    async def join_with_captcha(self, session, link, index, mode, report):
        """Join, then wait up to CAPTCHA_WAIT for a captcha and click it."""
        clicked = asyncio.Event()

        async def handler(msg):
            if await self.on_message(msg):
                clicked.set()

        # registered before joining: the captcha can arrive right after the join
        session.add_event_handler(handler, events.NewMessage)

        try:
            joined = await self.join(session, link, index, mode, report)

            if joined:
                await report(f"[аккаунт {index + 1}] вступил")
                try:
                    await asyncio.wait_for(clicked.wait(), CAPTCHA_WAIT)
                except asyncio.TimeoutError:
                    await report(f"[аккаунт {index + 1}] капчи не было {CAPTCHA_WAIT} с")
                else:
                    await report(f"[аккаунт {index + 1}] капча пройдена")

            return joined
        finally:
            session.remove_event_handler(handler, events.NewMessage)

    async def run(self, mode, link, report, captcha=False):
        """Join `mode` into `link` on every worker."""
        joined = 0
        self.progress_total(len(self.sessions))

        for index, session in enumerate(self.sessions):
            async with self.storage.ainitialize_session(session):
                if captcha:
                    is_joined = await self.join_with_captcha(session, link, index, mode, report)
                else:
                    is_joined = await self.join(session, link, index, mode, report)
                    if is_joined:
                        await report(f"[аккаунт {index + 1}] вступил")

                if is_joined:
                    joined += 1
                    self.progress_ok()
                else:  # join() reported why
                    self.progress_failed()

            self.progress_step()
            if index < len(self.sessions) - 1:  # no trailing wait after the last account
                await self.delay()

        await report(f"Итого: {joined}/{len(self.sessions)} аккаунтов")

    async def on_message(self, msg: types.Message):
        """Click the first callback button of a message mentioning this account; True if clicked."""
        if not msg.mentioned:
            return False
        # MessageButton.data: callback bytes, None for URL / reply / other buttons
        # (hides this layer's KeyboardButton(type=InlineButtonTypeCallback) layout)
        buttons = await msg.get_buttons()
        data = buttons[0][0].data if buttons and buttons[0] else None
        if data:
            await msg.click(data=data)  # raw bytes: the data need not be UTF-8
            return True
        return False
