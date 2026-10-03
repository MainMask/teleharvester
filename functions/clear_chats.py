import asyncio

from telethon import functions, types, TelegramClient
from rich.prompt import Confirm

from functions.base import TelethonFunction
from functions.base.base import console_report


class ClearDialogsFunc(TelethonFunction):
    """Clear all dialogs"""

    async def clear(self, session: TelegramClient, report):
        async with self.storage.ainitialize_session(session):
            async for dialog in session.iter_dialogs():
                try:
                    if not isinstance(dialog.entity, (types.Channel, types.ChannelForbidden)):
                        # Telegram deletes in chunks: offset > 0 means more history is left
                        offset = 1
                        while offset > 0:
                            result = await session(functions.messages.DeleteHistoryRequest(
                                peer=dialog.entity,
                                max_id=0,
                                just_clear=True,
                                revoke=True
                            ))
                            offset = result.offset
                    else:
                        # pass the (freshly iterated) Channel entity, not the marked id,
                        # so the InputChannel is built directly instead of via a cache lookup
                        # — matching the DeleteHistoryRequest branch above
                        await session(
                            functions.channels.LeaveChannelRequest(dialog.entity)
                        )
                except Exception as err:
                    await report(f"[!] {dialog.id}: {err}")
                    continue

                await report(f"Dialog {dialog.id} | {dialog.title} has been deleted")

    async def run(self, report):
        await asyncio.gather(*[
            self.clear(session, report)
            for session in self.sessions
        ])

    async def execute(self):
        self.ask_accounts_count()

        confirm = Confirm.ask("[bold red]are you sure?[/]")

        if confirm:
            await self.run(console_report)

