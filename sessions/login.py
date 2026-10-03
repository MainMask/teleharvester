import json
import sys

sys.path.append("..")

from telethon import events

from modules.storages.sessions_storage import SessionsStorage
from modules.types.json_session import JsonSession

if len(sys.argv) != 2:
    print("Usage: python login.py <session_file>")
    sys.exit(1)

name = sys.argv[1]

with open(name) as fileobj:
    session_settings = json.load(fileobj)

session = JsonSession(dict_settings=session_settings)

client = SessionsStorage.build_jsession_client(session)

with client:
    print("Mobile phone:", client.get_me().phone)


@client.on(events.NewMessage)
async def handler(msg):
    if msg.sender_id == 777000:  # incoming private messages have no from_id (layer 119+)
        print(msg.text)

client.start()
client.run_until_disconnected()
