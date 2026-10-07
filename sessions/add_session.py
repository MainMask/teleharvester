import sys

sys.path.append("..")

import asyncio

from modules.generators.linux import LinuxAPI
from modules.generators.telegram_android import TelegramAppAPI
from modules.types.json_session import JsonSession
from modules.types.proxy import Proxy

generators = {
    "1": TelegramAppAPI,
    "2": LinuxAPI,
    "3": None
}

proxy_types = {
    "1": "socks4",
    "2": "socks5",
    "3": "http",
    "4": None
}


print("[1] - Telegram Android")
print("[2] - Telegram Desktop (Linux)")
print("[3] - Случайно")

generator_choice = input(">> ")
generator = generators[generator_choice]

print("Прокси:")
print("[1] - Socks4")
print("[2] - Socks5")
print("[3] - HTTP")
print("[4] - Без прокси")

proxy_type_choice = input(">> ")
proxy_type = proxy_types[proxy_type_choice]

proxy = None

if proxy_type is not None:
    proxy_ip = input("IP прокси: ")
    proxy_port = int(input("Порт прокси: "))
    proxy_user = input("Логин прокси (пусто, если нет): ")
    proxy_password = input("Пароль прокси (пусто, если нет): ")
    
    proxy = Proxy(proxy_type, proxy_ip, proxy_port, proxy_user, proxy_password)

asyncio.run(
    JsonSession().create_application_session(generator, proxy)
)

