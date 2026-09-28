from functions.base import TelethonFunction
from functions.broadcast import Broadcast

class BroadcastChatFunc(TelethonFunction):
    """Broadcast to chat"""

    async def execute(self):
        broadcast = Broadcast(self.storage, self.settings)

        broadcast.ask()
        await broadcast.start_campaign()
