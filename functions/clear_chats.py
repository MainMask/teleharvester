
from telethon import functions, types, TelegramClient
from rich.prompt import Confirm

from functions.base import TelethonFunction
from functions.base.base import AccountLimited, console_report


class ClearDialogsFunc(TelethonFunction):
    """Clear all dialogs"""

    async def clear(self, session: TelegramClient, report):
        async with self.storage.ainitialize_session(session):
            try:
                await self.get_me(session)  # a worker that can't connect is skipped here, the others still run
            except Exception as err:
                await report(f"get_me failed: {err}")
                return

            try:
                async for dialog in session.iter_dialogs():
                    try:
                        if not isinstance(dialog.entity, (types.Channel, types.ChannelForbidden)):
                            # Telegram deletes in chunks: offset > 0 means more history is left
                            offset = 1
                            while offset > 0:
                                # safe_call waits out short FloodWaits (a mass wipe trips rate limits)
                                result = await self.safe_call(lambda: session(functions.messages.DeleteHistoryRequest(
                                    peer=dialog.entity,
                                    max_id=0,
                                    just_clear=True,
                                    revoke=True
                                )))
                                offset = result.offset
                        else:
                            # pass the (freshly iterated) Channel entity, not the marked id,
                            # so the InputChannel is built directly instead of via a cache lookup
                            # — matching the DeleteHistoryRequest branch above
                            await self.safe_call(lambda: session(
                                functions.channels.LeaveChannelRequest(dialog.entity)
                            ))
                    except AccountLimited as err:
                        # a long FloodWait: stop clearing this account, leave the others to run
                        await report(f"[!] limit, stopping. {err}")
                        return
                    except Exception as err:
                        await report(f"[!] {dialog.id}: {err}")
                        continue

                    await report(f"Dialog {dialog.id} | {dialog.title} has been deleted")
            except Exception as err:  # the listing itself (a long flood wait, a dropped connection): this worker only
                await report(f"[!] can't list dialogs: {err}")

    async def run(self, report):
        await self.gather_in_order(self.clear, report)

    async def execute(self):
        self.ask_accounts_count()

        confirm = Confirm.ask("[bold red]are you sure?[/]")

        if confirm:
            await self.run(console_report)

